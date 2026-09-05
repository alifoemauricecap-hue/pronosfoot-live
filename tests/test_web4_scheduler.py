# -*- coding: utf-8 -*-
"""
TESTS WEB-4 — SCHEDULER / CYCLES / RESTART / BACKUP / CANARY / INTÉGRATION
===========================================================================
Deux familles :
1. Cycles pilotés par un orchestrateur SIMULÉ (Rapports réels, 0 réseau) :
   T-180/T-60/T-15, kickoff protection, versionnage, entités, reprise
   START→INGEST→STOP→START (§23), backup/restore (§24), budget §10,
   intégrité 2A §33 (hashes gelés intacts), canary compare §35.
2. INTÉGRATION LOOPBACK : SafeHttpClient + cache PERSISTANT + vrai
   orchestrateur WEB-3 servi par un serveur local (fixtures) — aucune
   sortie réseau publique (garde anti-public active).
"""
import copy
import gzip
import json
import os
import socket
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import db as db_layer
import repository as repo
import prediction_service as predsvc
import engine
import app as appmod

from sources.web.normalized import UNKNOWN, new_datapoint, unknown_point
from sources.web.orchestrator import ResearchReport
from sources.web import snapshots as _w3snap  # noqa: F401 (référence pont)
from sources.web.web4 import bridge
from sources.web.web4 import diagnostics as w4d
from sources.web.web4 import metrics as w4m
from sources.web.web4.config import WEB4_CONFIG
from sources.web.web4.entity_store import EntityStore, team_key
from sources.web.web4.journal_sqlite import PersistentResearchJournal
from sources.web.web4.persistent_cache import WebCacheSQLite
from sources.web.web4.pit_sqlite import PersistentPITStore
from sources.web.web4.scheduler import IngestionScheduler
from sources.web.web4.venues import VenueStore

UTC = timezone.utc
SNOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
AS_OF = SNOW.isoformat()
CODE = "test.1"


# ===========================================================================
# OUTILS
# ===========================================================================
def _match_row(mid, minutes, ext_home="361", ext_away="349", comp="eng.1",
               venue=None):
    return {"id": mid, "source": "espn",
            "source_match_id": mid.split(":")[-1],
            "competition": comp, "home_team": "Home FC", "away_team": "Away FC",
            "home_team_ext_id": ext_home, "away_team_ext_id": ext_away,
            "kickoff_time_utc": (SNOW + timedelta(minutes=minutes))
            .replace(tzinfo=None).isoformat() + "Z",
            "venue": venue, "status": "UPCOMING"}


def _insert_match(row):
    repo.upsert_match(row)
    return row


def make_report(mid, requests=4, value=8, identity=None, errors=None,
                retrieved=AS_OF):
    rep = ResearchReport(mid, AS_OF)
    rep.points = [new_datapoint(value, "shots", "espn", retrieved,
                                match_id=mid)]
    rep.requests_used = requests
    rep.identity = identity
    rep.errors = errors or []
    return rep


class FakeOrch:
    """Orchestrateur simulé : rapports RÉELS préparés, aucune I/O."""

    def __init__(self, reports=None, raise_for=None):
        self.reports = reports or {}
        self.raise_for = set(raise_for or ())
        self.calls = []

    def research_match(self, ctx, as_of=None):
        mid = ctx["match_id"]
        self.calls.append(mid)
        if mid in self.raise_for:
            raise RuntimeError("source en panne (simulée)")
        return self.reports.get(mid) or ResearchReport(mid, as_of or AS_OF)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    p = str(tmp_path / "w4.db")
    db_layer.init(path=p, reset=True)
    repo.register_model(engine.MODEL_NAME, engine.MODEL_VERSION,
                        engine.MODEL_CONFIG)
    with predsvc._MEM:
        predsvc._PUB.clear(); predsvc._DISP.clear()
        predsvc._T15.clear(); predsvc._DONE.clear()
    appmod._MATCH_MEM.clear()
    w4d.set_runtime(scheduler=None, client=None, cache=None)
    yield p


def _sched(orch, config=None):
    cfg = copy.deepcopy(WEB4_CONFIG)
    if config:
        cfg.update(config)
    return IngestionScheduler(lambda: orch, PersistentPITStore(),
                              PersistentResearchJournal(),
                              metrics=w4m.IngestionMetrics(), config=cfg,
                              clock=lambda: SNOW)


def _cycles():
    return db_layer.rows_to_dicts(db_layer.query(
        "SELECT * FROM web_ingestion_cycles ORDER BY started_at"))


# ===========================================================================
# 1. CYCLES GROUPES (§8)
# ===========================================================================
class TestCycles:
    def test_01_cycle_row_complete(self):
        row = _insert_match(_match_row("espn:eng.1:M1", 200))
        orch = FakeOrch({row["id"]: make_report(row["id"])})
        s = _sched(orch)
        out = s.run_cycle(trigger="MANUAL")
        assert out["status"] == "DONE"
        cyc = _cycles()[0]
        assert cyc["trigger"] == "MANUAL" and cyc["status"] == "DONE"
        assert cyc["match_count"] == 1 and cyc["request_count"] == 4
        assert cyc["finished_at"] and cyc["duration_ms"] is not None

    def test_02_datapoints_persistes_au_cycle(self):
        row = _insert_match(_match_row("espn:eng.1:M2", 200))
        orch = FakeOrch({row["id"]: make_report(row["id"])})
        _sched(orch).run_cycle()
        pts = PersistentPITStore().query(match_id=row["id"])
        assert len(pts) == 1 and pts[0].value == 8

    def test_03_metrics_comptees(self):
        row = _insert_match(_match_row("espn:eng.1:M3", 200))
        orch = FakeOrch({row["id"]: make_report(row["id"])})
        _sched(orch).run_cycle()
        snap = w4m.IngestionMetrics().snapshot()
        assert snap["ingestion_cycles"] == 1
        assert snap["successful_cycles"] == 1
        assert snap["source_requests"] == 4

    def test_04_resilience_un_match_en_panne(self):
        r1 = _insert_match(_match_row("espn:eng.1:M4", 200))
        r2 = _insert_match(_match_row("espn:eng.1:M5", 300))
        orch = FakeOrch({r2["id"]: make_report(r2["id"])},
                        raise_for={r1["id"]})
        out = _sched(orch).run_cycle(matches=[r1, r2])
        assert out["status"] == "DONE_WITH_ERRORS"      # §22 : cycle continue
        assert r2["id"] in out["matches"]               # M5 traité malgré M4

    def test_05_aucune_exception_ne_sort_du_cycle(self):
        orch = FakeOrch(raise_for={"X"})
        row = _insert_match(_match_row("espn:eng.1:X", 200))
        out = _sched(orch).run_cycle(matches=[row])
        assert out["status"] in ("DONE", "DONE_WITH_ERRORS")


# ===========================================================================
# 2. BUDGET RÉSEAU (§10) — jamais augmenté
# ===========================================================================
class TestBudget:
    def test_06_requests_per_match_over_hard_cap_alerte_ERROR(self):
        row = _insert_match(_match_row("espn:eng.1:B1", 200))
        orch = FakeOrch({row["id"]: make_report(row["id"], requests=21)})
        out = _sched(orch).run_cycle(matches=[row])
        assert out["status"] == "DONE_WITH_ERRORS"
        codes = [a["code"] for a in w4m.recent_alerts(10)]
        assert "REQUESTS_PER_MATCH_HARD_CAP" in codes
        lv = [a["level"] for a in w4m.recent_alerts(10)
              if a["code"] == "REQUESTS_PER_MATCH_HARD_CAP"]
        assert lv[0] == "ERROR"

    def test_07_requests_9_a_20_alerte_WARNING(self):
        row = _insert_match(_match_row("espn:eng.1:B2", 200))
        orch = FakeOrch({row["id"]: make_report(row["id"], requests=9)})
        _sched(orch).run_cycle(matches=[row])
        codes = [a["code"] for a in w4m.recent_alerts(10)]
        assert "REQUESTS_PER_MATCH_OVER_BUDGET" in codes

    def test_08_cap_du_cycle_regroupe(self):
        rows = [_insert_match(_match_row(f"espn:eng.1:B{i}", 200 + i))
                for i in range(3, 6)]
        orch = FakeOrch({r["id"]: make_report(r["id"], requests=4)
                         for r in rows})
        out = _sched(orch, {"requests_per_cycle_cap": 5}).run_cycle(
            matches=rows)
        assert out["requests"] <= 8
        assert any(s["reason"] == "CYCLE_CAP" for s in out["skipped"])

    def test_09_canary_max_5_matchs_selection(self):
        for i in range(8):
            _insert_match(_match_row(f"espn:eng.1:C{i}", 100 + i))
        out = _sched(FakeOrch()).run_cycle(trigger="SCHEDULED")
        assert len(out["matches"]) <= WEB4_CONFIG["matches_per_cycle"]


# ===========================================================================
# 3. CANDIDATS T-180 / T-60 / T-15 + PROTECTIONS (§13/§14/§15/§16)
# ===========================================================================
class TestCandidates:
    def _run_window(self, minutes, expected_window):
        row = _insert_match(_match_row(f"espn:eng.1:W{minutes}", minutes))
        orch = FakeOrch({row["id"]: make_report(row["id"])})
        out = _sched(orch).run_cycle(matches=[row])
        return out, row

    def test_10_t180_cree_un_candidat(self):
        out, row = self._run_window(170, "T-180")
        assert out["candidates"][0]["t_window"] == "T-180"
        assert out["candidates"][0]["created"] is True
        cands = bridge.candidates_for_match(row["id"])
        assert len(cands) == 1

    def test_11_t60_cree_un_candidat(self):
        out, row = self._run_window(50, "T-60")
        assert out["candidates"][0]["t_window"] == "T-60"

    def test_12_t15_cree_un_candidat(self):
        out, row = self._run_window(10, "T-15")
        assert out["candidates"][0]["t_window"] == "T-15"

    def test_13_horizon_aucun_candidat(self):
        out, row = self._run_window(200, None)
        assert out["candidates"] == []
        assert bridge.candidates_for_match(row["id"]) == []

    def test_14_apres_kickoff_aucun_candidat(self):
        row = _match_row("espn:eng.1:POST", -30)          # §15/§16
        row["status"] = "LIVE"
        repo.upsert_match(row)
        orch = FakeOrch({row["id"]: make_report(row["id"])})
        out = _sched(orch).run_cycle(matches=[row])
        assert out["candidates"] == []
        assert db_layer.query("SELECT COUNT(*) AS c FROM data_snapshots "
                              "WHERE source='web4-candidate'",
                              one=True)["c"] == 0

    def test_15_les_3_versions_coexistent(self):
        """§13/§14 — v1 (T-180), v2 (T-60), v3 (T-15) : chacune indépendante
        et conservée, jamais écrasée."""
        row = _insert_match(_match_row("espn:eng.1:V3", 170))
        base_orch = FakeOrch({row["id"]: make_report(row["id"], value=8)})
        _sched(base_orch).run_cycle(matches=[row])
        # T-60 : nouvelles données (value change → nouvelle observation)
        db_layer.execute("UPDATE matches SET kickoff_time_utc=%s WHERE id=%s",
                         ((SNOW + timedelta(minutes=50)).replace(tzinfo=None)
                          .isoformat() + "Z", row["id"]))
        row2 = repo.get_match(row["id"])
        orch2 = FakeOrch({row["id"]: make_report(row["id"], value=10)})
        _sched(orch2).run_cycle(matches=[row2])
        # T-15
        db_layer.execute("UPDATE matches SET kickoff_time_utc=%s WHERE id=%s",
                         ((SNOW + timedelta(minutes=10)).replace(tzinfo=None)
                          .isoformat() + "Z", row["id"]))
        row3 = repo.get_match(row["id"])
        orch3 = FakeOrch({row["id"]: make_report(row["id"], value=12)})
        _sched(orch3).run_cycle(matches=[row3])
        cands = bridge.candidates_for_match(row["id"])
        assert len(cands) == 3
        wins = sorted(json.loads(c["payload_hash"] and
                      db_layer.query("SELECT payload_json FROM data_snapshots "
                                     "WHERE id=%s", (c["id"],),
                                     one=True)["payload_json"])["t_window"]
                      for c in cands)
        assert wins == ["T-15", "T-180", "T-60"]
        # versionnage PIT : 8, 10, 12 tous présents (§6)
        vals = {p.value for p in PersistentPITStore().query(
            match_id=row["id"], data_type="shots")}
        assert vals == {8, 10, 12}

    def test_16_candidat_identique_pas_de_doublon(self):
        row = _insert_match(_match_row("espn:eng.1:DD", 170))
        orch = FakeOrch({row["id"]: make_report(row["id"])})
        _sched(orch).run_cycle(matches=[row])
        _sched(orch).run_cycle(matches=[row])            # mêmes données
        assert len(bridge.candidates_for_match(row["id"])) == 1

    def test_17_predictions_jamais_touchees_par_le_cycle(self):
        out, row = self._run_window(170, "T-180")
        assert db_layer.table_counts()["predictions"] == 0


# ===========================================================================
# 4. ENTITY MAP SEEDING (§17/§18)
# ===========================================================================
class TestEntitySeeding:
    def test_18_identite_auto_cree_mappings(self):
        row = _insert_match(_match_row("espn:eng.1:E1", 200))
        ident = {"status": "MATCH_AUTO", "confidence": 0.96}
        orch = FakeOrch({row["id"]: make_report(row["id"], identity=ident)})
        _sched(orch).run_cycle(matches=[row])
        es = EntityStore()
        assert es.count() == 2
        assert es.resolve_team("Home FC", "espn") == "361"
        assert es.resolve_team("Away FC", "espn") == "349"

    def test_19_identite_faible_aucun_mapping(self):
        row = _insert_match(_match_row("espn:eng.1:E2", 200))
        ident = {"status": "UNKNOWN", "confidence": 0.5}
        orch = FakeOrch({row["id"]: make_report(row["id"], identity=ident)})
        _sched(orch).run_cycle(matches=[row])
        assert EntityStore().count() == 0

    def test_20_contradiction_ulterieure_preservee(self):
        row = _insert_match(_match_row("espn:eng.1:E3", 200))
        ident = {"status": "MATCH_AUTO", "confidence": 0.96}
        orch = FakeOrch({row["id"]: make_report(row["id"], identity=ident)})
        _sched(orch).run_cycle(matches=[row])
        row2 = _match_row("espn:eng.1:E4", 300, ext_home="999")
        _insert_match(row2)
        orch2 = FakeOrch({row2["id"]: make_report(row2["id"],
                                                  identity=ident)})
        _sched(orch2).run_cycle(matches=[row2])
        es = EntityStore()
        assert es.resolve_team("Home FC", "espn") == "361"   # intact
        alerts = [a for a in w4m.recent_alerts(10)
                  if a["code"] == "ENTITY_CONTRADICTION"]
        assert alerts


# ===========================================================================
# 5. RESTART §23 — START → INGEST → STOP → START
# ===========================================================================
class TestRestart:
    def test_21_restart_hashes_identiques_et_sans_duplication(self):
        row = _insert_match(_match_row("espn:eng.1:R1", 170))
        reports = {row["id"]: make_report(row["id"])}
        # START 1
        _sched(FakeOrch(reports)).run_cycle(matches=[row])
        before_snaps = db_layer.rows_to_dicts(db_layer.query(
            "SELECT payload_hash FROM data_snapshots ORDER BY id"))
        n_points = PersistentPITStore().count()
        n_entities = EntityStore().count()
        # « STOP » = nouvelles instances sur le MÊME fichier ; START 2
        _sched(FakeOrch(reports)).run_cycle(matches=[row])
        after_snaps = db_layer.rows_to_dicts(db_layer.query(
            "SELECT payload_hash FROM data_snapshots ORDER BY id"))
        assert before_snaps == after_snaps               # hashes IDENTIQUES
        assert PersistentPITStore().count() == n_points  # 0 duplication
        assert EntityStore().count() == n_entities

    def test_22_nouvelle_observation_apres_restart_est_une_version(self):
        row = _insert_match(_match_row("espn:eng.1:R2", 200))
        _sched(FakeOrch({row["id"]: make_report(row["id"], value=8)})
               ).run_cycle(matches=[row])
        _sched(FakeOrch({row["id"]: make_report(row["id"], value=10)})
               ).run_cycle(matches=[row])
        vals = sorted(p.value for p in PersistentPITStore()
                      .query(match_id=row["id"]))
        assert vals == [8, 10]                           # §6 versionnage

    def test_23_recover_on_boot_apres_inactivite(self):
        out = _sched(FakeOrch()).recover_on_boot()
        assert out["recovered"] is True
        cyc = _cycles()[0]
        assert cyc["trigger"] == "RECOVERY"

    def test_24_recover_inutile_si_cycle_frais(self):
        s = _sched(FakeOrch())
        s.recover_on_boot()
        out = s.recover_on_boot()
        assert out["recovered"] is False and out["reason"] == "FRESH"

    def test_25_journal_survit_au_restart(self):
        row = _insert_match(_match_row("espn:eng.1:R3", 170))   # T-180 : le
        _sched(FakeOrch({row["id"]: make_report(row["id"])})    # bridge écrit
               ).run_cycle(matches=[row])                       # au journal
        j2 = PersistentResearchJournal()                 # « restart »
        assert j2.count() >= 1
        evs = j2.events(match_id=row["id"])
        assert any(e["event"] == "SNAPSHOT_CANDIDATE_CREATED" for e in evs)


# ===========================================================================
# 6. BACKUP / RESTORE (§24) — hashes 2A identiques après restore
# ===========================================================================
class TestBackupRestore:
    def test_26_backup_restore_base_complete(self, tmp_path):
        row = _insert_match(_match_row("espn:eng.1:BK1", 170))
        _sched(FakeOrch({row["id"]: make_report(row["id"])})
               ).run_cycle(matches=[row])
        src = db_layer.db_path()
        dst = str(tmp_path / "restore.db")
        scon = sqlite3.connect(src)
        dcon = sqlite3.connect(dst)
        with dcon:
            scon.backup(dcon)
        scon.close(); dcon.close()
        before = db_layer.rows_to_dicts(db_layer.query(
            "SELECT id, payload_hash FROM data_snapshots ORDER BY id"))
        db_layer.init(path=dst, reset=True)
        after = db_layer.rows_to_dicts(db_layer.query(
            "SELECT id, payload_hash FROM data_snapshots ORDER BY id"))
        assert before == after                            # hashes IDENTIQUES
        for t in ("web_cache", "web_datapoints", "web_research_events",
                  "web_ingestion_cycles", "web_entities", "web_venues",
                  "web_metrics", "web_scheduler_state", "web_alerts"):
            assert db_layer.query(
                f"SELECT COUNT(*) AS c FROM {t}", one=True)["c"] >= 0
        assert PersistentPITStore().count() == 1

    def test_27_restore_gzip_roundtrip(self, tmp_path):
        row = _insert_match(_match_row("espn:eng.1:BK2", 170))
        _sched(FakeOrch({row["id"]: make_report(row["id"])})
               ).run_cycle(matches=[row])
        src = open(db_layer.db_path(), "rb").read()
        gzpath = tmp_path / "bak.gz"
        gzpath.write_bytes(gzip.compress(src))
        dst = str(tmp_path / "gunzipped.db")
        with open(dst, "wb") as f:
            f.write(gzip.decompress(gzpath.read_bytes()))
        db_layer.init(path=dst, reset=True)
        assert db_layer.integrity_check()                 # intègre
        # v2 WEB-4 appliquée (la v3 2C, additive, fait monter le max à 3 :
        # l'exigence historique « v2 présente » est préservée, non affaiblie)
        assert db_layer.migration_version() >= 2


# ===========================================================================
# 7. INTÉGRITÉ 2A (§33) — le socle est intact après les cycles
# ===========================================================================
def _team(tid, sw, gf, ga):
    return {"id": tid, "name": f"T{tid}", "logo": None, "n": 12, "sw": sw,
            "gf": gf, "ga": ga, "home_n": 6, "home_sw": sw / 2,
            "home_gf": gf / 2, "home_ga": ga / 2, "away_n": 6,
            "away_sw": sw / 2, "away_gf": gf / 2, "away_ga": ga / 2,
            "w": 6, "d": 2, "l": 4, "pts": 20,
            "form": [{"r": "W", "gf": 2, "ga": 1, "day": "2026-08-30",
                      "loc": "D", "opp": "X"}], "matches": []}


def _make_ev(eid="E1", hours=3, state="pre"):
    ko = (datetime.now(UTC) + timedelta(hours=hours)).isoformat()
    return {"id": eid, "utc": ko, "day": ko[:10], "state": state,
            "completed": False,
            "home": {"id": "H", "name": "Home", "full": "Home FC",
                     "logo": None, "score": None, "ht": None},
            "away": {"id": "A", "name": "Away", "full": "Away FC",
                     "logo": None, "score": None, "ht": None},
            "venue": "Stade Test", "clock": "", "clockSec": 0, "detail": ""}


class TestIntegrity2A:
    def _publish_2a(self):
        stats = {"teams": {"H": _team("H", 8.0, 14.5, 8.0),
                           "A": _team("A", 6.0, 6.5, 9.0)},
                 "league": {"n": 100, "homeAvg": 1.45, "awayAvg": 1.10}}
        ev = _make_ev("E2A")
        appmod.persist_match(CODE, ev)
        disp = predsvc.publish_match(CODE, ev, stats, None)
        assert disp and disp["frozen"] is True
        return f"espn:{CODE}:E2A"

    def test_28_prediction_gelee_survit_aux_cycles(self):
        mid = self._publish_2a()
        preds_before = repo.predictions_history(mid)
        row = repo.get_match(mid)
        orch = FakeOrch({mid: make_report(mid)})
        _sched(orch).run_cycle(matches=[row])
        preds_after = repo.predictions_history(mid)
        assert preds_before == preds_after
        for p in preds_after:
            assert predsvc.verify_prediction(p) is True
        snap2a = db_layer.query("SELECT COUNT(*) AS c FROM data_snapshots "
                                "WHERE match_id=%s AND source='espn'",
                                (mid,), one=True)["c"]
        assert snap2a == 1                               # snapshot 2A intact

    def test_29_canary_compare_sans_ecraser(self):
        mid = self._publish_2a()
        # ancre temporelle du scheduler : kickoff = SNOW + 170 min (T-180)
        db_layer.execute(
            "UPDATE matches SET kickoff_time_utc=%s WHERE id=%s",
            ((SNOW + timedelta(minutes=170)).replace(tzinfo=None)
             .isoformat() + "Z", mid))
        row = repo.get_match(mid)
        orch = FakeOrch({mid: make_report(mid)})
        _sched(orch).run_cycle(matches=[row], activate_candidates=True)
        cmp_ = bridge.compare_with_2a(mid)
        assert cmp_["snapshot_2a"] is not None
        assert cmp_["candidate_web4"] is not None        # candidat à côté
        assert cmp_["diff"]["hash_equal"] is False       # 2 mondes séparés
        # le snapshot 2A n'a JAMAIS été remplacé (§35)
        per_src = db_layer.rows_to_dicts(db_layer.query(
            "SELECT source, COUNT(*) AS c FROM data_snapshots "
            "WHERE match_id=%s GROUP BY source", (mid,)))
        counts = {r["source"]: r["c"] for r in per_src}
        assert counts.get("espn") == 1 and counts.get("web4-candidate") == 1

    def test_30_integrity_scan_zero_corruption(self):
        self._publish_2a()
        out = bridge.integrity_scan(limit=50)
        assert out["checked"] == 6 and out["corrupted"] == []


# ===========================================================================
# 8. DIAGNOSTIC (§30/§31) + santé
# ===========================================================================
class TestDiagnostics:
    def test_31_ingestion_status_shape(self):
        row = _insert_match(_match_row("espn:eng.1:D1", 170))
        s = _sched(FakeOrch({row["id"]: make_report(row["id"])}))
        w4d.set_runtime(scheduler=s)
        s.run_cycle(matches=[row])
        st = w4d.ingestion_status()
        for k in ("ok", "core_health", "data_health", "scheduler", "cache",
                  "cycles", "sources", "snapshots", "alerts"):
            assert k in st
        assert st["cycles"]["last"]["match_count"] == 1
        assert st["cycles"]["matches_processed"] == 1
        assert st["cache"]["persistent_entries"] >= 0
        assert st["snapshots"]["candidates"] == 1

    def test_32_data_health_ok_puis_degraded(self):
        row = _insert_match(_match_row("espn:eng.1:D2", 200))
        _sched(FakeOrch({row["id"]: make_report(row["id"])})).run_cycle(
            matches=[row])
        assert w4d.data_health() == "ok"
        _sched(FakeOrch(raise_for={row["id"]})).run_cycle(matches=[row])
        assert w4d.data_health() == "degraded"

    def test_33_aucun_secret_dans_le_diagnostic(self):
        row = _insert_match(_match_row("espn:eng.1:D3", 200))
        _sched(FakeOrch({row["id"]: make_report(row["id"])})).run_cycle(
            matches=[row])
        txt = json.dumps(w4d.ingestion_status(), ensure_ascii=False)
        for bad in ("authorization", "x-auth-token", "api_key", "password"):
            assert bad not in txt.lower()

    def test_34_healthz_route_flask(self):
        c = appmod.app.test_client()
        h = c.get("/healthz").get_json()
        assert h["ok"] is True and h["db"]["integrity"] is True
        assert "ingestion" in h
        assert h["ingestion"]["core_health"] == "ok"
        # idem : migration v3 (2C, additive) — v2 toujours présente (>= 2)
        assert h["db"]["migration"] >= 2

    def test_35_api_ingestion_status_route(self):
        c = appmod.app.test_client()
        r = c.get("/api/ingestion/status")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True and body["core_health"] == "ok"


# ===========================================================================
# 9. LIVE / SSE INTACTS (§25/§26)
# ===========================================================================
class TestLiveSseIntact:
    def test_36_routes_live_existantes(self):
        rules = {r.rule for r in appmod.app.url_map.iter_rules()}
        for route in ("/api/stream", "/api/feed", "/api/config",
                      "/api/predictions/history"):
            assert route in rules

    def test_37_boucles_live_inchangees_presentes(self):
        for fn in ("feed_loop", "live_detail_loop", "prematch_detail_loop",
                   "refresh_state", "sse_broadcast"):
            assert callable(getattr(appmod, fn))

    def test_38_start_web4_est_daemon_et_non_bloquant(self):
        import os
        os.environ["PRONOFOOT_WEB4"] = "1"
        sched = appmod.start_web4()
        assert sched is not None
        sched.stop()

    def test_39_start_web4_desactivable(self):
        import os
        os.environ["PRONOFOOT_WEB4"] = "0"
        assert appmod.start_web4() is None
        os.environ["PRONOFOOT_WEB4"] = "1"


# ===========================================================================
# 10. INTÉGRATION LOOPBACK — cache PERSISTANT + vrai pipeline (0 public)
# ===========================================================================
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "web3")
_REAL_GAI = socket.getaddrinfo
PUBLIC_ATTEMPTS = []


@pytest.fixture(autouse=True)
def _no_public_network(monkeypatch):
    def guard(host, port, *a, **k):
        if (host or "") in ("127.0.0.1", "localhost", "::1"):
            return _REAL_GAI(host, port, *a, **k)
        PUBLIC_ATTEMPTS.append(host)
        raise AssertionError(f"PUBLIC INTERDIT : {host!r}")
    monkeypatch.setattr(socket, "getaddrinfo", guard)


def _fx(name):
    return open(os.path.join(FIX, name), "rb").read()


_STANDINGS = json.dumps({"children": []}).encode()

ROUTES = {
    "/apis/site/v2/sports/soccer/eng.1/scoreboard":
        (_fx("espn_scoreboard_real.json"), "application/json"),
    "/apis/site/v2/sports/soccer/eng.1/summary":
        (_fx("espn_summary_fixture.json"), "application/json"),
    "/apis/site/v2/sports/soccer/eng.1/standings":
        (_STANDINGS, "application/json"),
    "/mmz4281/2627/E0.csv": (_fx("fdco_E0_sample_real.csv"), "text/csv"),
    "/statsbomb/open-data/master/data/competitions.json":
        (_fx("sb_competitions_real.json"), "application/json"),
    "/w/api.php": (_fx("wikidata_search_psg_real.json"), "application/json"),
}

HITS = {}
HITS_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        with HITS_LOCK:
            HITS[path] = HITS.get(path, 0) + 1
        route = ROUTES.get(path)
        if route is None:
            body = b"{}"
            self._send(404, body, "application/json")
            return
        body, ctype = route
        self._send(200, body, ctype)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


_PATH_PREFIX = {
    "espn": "/apis/site/v2/sports/soccer",
    "football_data_co_uk": "/mmz4281",
    "open_meteo": "/v1",
    "openligadb": "",
    "statsbomb_open": "/statsbomb/open-data/master/data",
    "wikidata": "",
}


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"port": port}
    srv.shutdown()
    srv.server_close()


def _rebranch(port):
    from sources import registry as regmod
    reg = copy.deepcopy(regmod.load())
    for src in reg["sources"]:
        sid = src["source_id"]
        if sid in _PATH_PREFIX:
            src["base_url"] = f"http://127.0.0.1:{port}{_PATH_PREFIX[sid]}"
    return reg


def _mk_stack(port):
    """Stack de production rebranchée loopback : client + cache persistant +
    orchestrateur réel + store/journal persistants (même DB que le test)."""
    from sources.web.compliance import ComplianceGate
    from sources.web.safe_http import SafeHttpClient
    from sources.web.orchestrator import ResearchOrchestrator
    from sources.web.quality_metrics import QualityMetrics
    reg = _rebranch(port)
    ov = {sid: {"allow_http": True, "allow_private_hosts": True}
          for sid in _PATH_PREFIX}
    gate = ComplianceGate(reg, ov)
    cache = WebCacheSQLite()
    client = SafeHttpClient(registry=reg, gate=gate, cache=cache, config={
        "rate_limit": {"per_source_min_interval_sec": 0.0,
                       "cycle_max_requests": 100000,
                       "cycle_window_sec": 86400.0},
        "retry": {"delay_sec": 0.01}})
    store = PersistentPITStore()
    journal = PersistentResearchJournal()
    qm = QualityMetrics()

    def factory():
        return ResearchOrchestrator(client, registry=reg, store=store,
                                    journal=journal, metrics=qm)
    return {"reg": reg, "client": client, "cache": cache, "store": store,
            "journal": journal, "factory": factory}


class TestLoopbackIntegration:
    def test_40_hit_puis_cache_persistant(self, server):
        st = _mk_stack(server["port"])
        r1 = st["client"].fetch(
            "espn",
            f"http://127.0.0.1:{server['port']}"
            "/apis/site/v2/sports/soccer/eng.1/scoreboard",
            "score_live")
        assert r1.ok and r1.cache_hit is False
        assert len(st["cache"]) == 1
        r2 = st["client"].fetch(
            "espn",
            f"http://127.0.0.1:{server['port']}"
            "/apis/site/v2/sports/soccer/eng.1/scoreboard",
            "score_live")
        assert r2.cache_hit is True
        assert r2.retrieved_at == r1.retrieved_at       # ORIGINAL §4
        with HITS_LOCK:
            assert HITS["/apis/site/v2/sports/soccer/eng.1/scoreboard"] == 1

    def test_41_cache_survit_nouveau_client(self, server):
        st = _mk_stack(server["port"])
        url = (f"http://127.0.0.1:{server['port']}"
               "/apis/site/v2/sports/soccer/eng.1/standings")
        st["client"].fetch("espn", url, "standings")
        st2 = _mk_stack(server["port"])                  # « restart » client
        r = st2["client"].fetch("espn", url, "standings")
        assert r.cache_hit is True
        with HITS_LOCK:
            assert HITS["/apis/site/v2/sports/soccer/eng.1/standings"] == 1

    def test_42_expiration_declenche_reseau(self, server):
        st = _mk_stack(server["port"])
        url = (f"http://127.0.0.1:{server['port']}"
               "/apis/site/v2/sports/soccer/eng.1/summary")
        st["client"].fetch("espn", url, "match_stats")
        db_layer.execute("UPDATE web_cache SET expires_at=0")
        r = st["client"].fetch("espn", url, "match_stats")
        assert r.cache_hit is False                      # miss ⇒ réseau
        with HITS_LOCK:
            assert HITS["/apis/site/v2/sports/soccer/eng.1/summary"] == 2

    def test_43_cycle_complet_puis_restart_zero_requete(self, server):
        """§23 poussée au bout : cycle RÉEL loopback → « restart » → 2e cycle
        = 100 % cache hits, AUCUNE requête réseau supplémentaire, points NON
        dupliqués (versionnage exact)."""
        mid = "espn:eng.1:401879286"
        row = {"id": mid, "source": "espn", "source_match_id": "401879286",
               "competition": "eng.1", "home_team": "Newcastle United",
               "away_team": "AFC Bournemouth",
               "home_team_ext_id": "361", "away_team_ext_id": "349",
               "kickoff_time_utc": (datetime.now(UTC) + timedelta(hours=5))
               .isoformat(), "venue": None, "status": "UPCOMING"}
        _insert_match(row)
        st = _mk_stack(server["port"])
        sched = IngestionScheduler(st["factory"], st["store"], st["journal"],
                                   metrics=w4m.IngestionMetrics(),
                                   registry=st["reg"])
        out1 = sched.run_cycle(trigger="MANUAL",
                               matches=[row], activate_candidates=True)
        assert out1["status"] in ("DONE", "DONE_WITH_ERRORS")
        assert out1["requests"] >= 1                     # du réseau réel (loopback)
        assert mid in out1["matches"]
        n_points = st["store"].count()
        n_hits_total = sum(HITS.values())
        # « restart » : NOUVELLE stack complète, même base
        st2 = _mk_stack(server["port"])
        sched2 = IngestionScheduler(st2["factory"], st2["store"],
                                    st2["journal"],
                                    metrics=w4m.IngestionMetrics(),
                                    registry=st2["reg"])
        out2 = sched2.run_cycle(trigger="MANUAL",
                                matches=[row], activate_candidates=True)
        assert out2["requests"] == 0                     # 100 % cache (§4)
        assert sum(HITS.values()) == n_hits_total        # 0 réseau ajouté
        # points RE-extraits des réponses cachées = nouvelles observations
        # horodatées du 2e run (versionnage §6 — jamais d'écrasement) ;
        # l'exacte déduplication est prouvée par test_B04 et test_21.
        assert st2["store"].count() >= n_points
        assert out2["cache_hits"] >= 1

    def test_44_cycle_loopback_cree_candidat_fonctionnel(self, server):
        mid = "espn:eng.1:401879286"
        ko = (datetime.now(UTC) + timedelta(minutes=170)).isoformat()
        row = {"id": mid, "source": "espn",
               "source_match_id": "401879286", "competition": "eng.1",
               "home_team": "Newcastle United",
               "away_team": "AFC Bournemouth",
               "home_team_ext_id": "361", "away_team_ext_id": "349",
               "kickoff_time_utc": ko, "venue": None,
               "status": "UPCOMING"}
        _insert_match(row)
        st = _mk_stack(server["port"])
        sched = IngestionScheduler(st["factory"], st["store"], st["journal"],
                                   metrics=w4m.IngestionMetrics(),
                                   registry=st["reg"])
        out = sched.run_cycle(matches=[row])
        cands = bridge.candidates_for_match(mid)
        assert out["candidates"] and cands
        payload = json.loads(db_layer.query(
            "SELECT payload_json FROM data_snapshots WHERE id=%s",
            (cands[0]["id"],), one=True)["payload_json"])
        assert payload["t_window"] == "T-180"
        assert "identity_decision" in payload["data"]    # preuve d'identité

    def test_45_meteo_jamais_requise_sans_stade_prouve(self, server):
        """§19 vérifié BOUT EN BOUT : venue=None ⇒ Open-Meteo n'est JAMAIS
        appelé (aucune approximation de ville)."""
        mid = "espn:eng.1:401879286"
        row = {"id": mid, "source": "espn",
               "source_match_id": "401879286", "competition": "eng.1",
               "home_team": "Newcastle United",
               "away_team": "AFC Bournemouth",
               "home_team_ext_id": "361", "away_team_ext_id": "349",
               "kickoff_time_utc": (datetime.now(UTC) + timedelta(hours=5))
               .isoformat(), "venue": None, "status": "UPCOMING"}
        _insert_match(row)
        st = _mk_stack(server["port"])
        sched = IngestionScheduler(st["factory"], st["store"], st["journal"],
                                   metrics=w4m.IngestionMetrics(),
                                   registry=st["reg"])
        sched.run_cycle(matches=[row])
        assert "/v1/forecast" not in HITS

    def test_46_sources_bloquees_jamais_contactees(self, server):
        """§38 — même dans un cycle complet, les sources BLOCKED (sofascore,
        flashscore…) ne sont JAMAIS résolues."""
        mid = "espn:eng.1:401879286"
        row = {"id": mid, "source": "espn",
               "source_match_id": "401879286", "competition": "eng.1",
               "home_team": "Newcastle United",
               "away_team": "AFC Bournemouth",
               "home_team_ext_id": "361", "away_team_ext_id": "349",
               "kickoff_time_utc": (datetime.now(UTC) + timedelta(hours=5))
               .isoformat(), "venue": None, "status": "UPCOMING"}
        _insert_match(row)
        st = _mk_stack(server["port"])
        sched = IngestionScheduler(st["factory"], st["store"], st["journal"],
                                   metrics=w4m.IngestionMetrics(),
                                   registry=st["reg"])
        sched.run_cycle(matches=[row])
        assert PUBLIC_ATTEMPTS == []
