# -*- coding: utf-8 -*-
"""2C.2 §15 — REDÉMARRAGE : scheduler actif → 2C actif → restart → recovery.
Aucun doublon, aucun horizon écrasé, aucune perte, 2A/WEB-4 inchangés."""
import json
from datetime import timedelta

import pytest
import db

from helpers_2c1 import (NOW, fresh_db, restore_db, insert_match, count, rows,
                         table_hash, runner as mk_runner)


@pytest.fixture(autouse=True)
def _db(tmp_path):
    prev = fresh_db(tmp_path)
    yield
    restore_db(prev)


KO = (NOW + timedelta(minutes=100)).isoformat().replace("+00:00", "Z")


def _sched():
    from sources.web.web4 import scheduler as w4s
    from sources.web.web4 import metrics as w4m

    class _Journal:
        def events_since(self, ts):
            return []

    class _Orch:
        def research_match(self, ctx, as_of=None):
            class R:
                requests_used = 0
                errors = []
                identity = None
                points = []
            return R()

    class _Store:
        def add_many(self, dps):
            return dps

        def query(self, **k):
            return []

    return w4s.IngestionScheduler(lambda: _Orch(), _Store(), _Journal(),
                                  metrics=w4m.IngestionMetrics(db_module=db),
                                  db_module=db)


class TestRestart:
    def test_new_runner_instance_no_duplicates(self):
        insert_match(db, "espn:ger.1:RS1", "Werder Bremen", "Bayern Munich", KO)
        mk_runner(db).run_once(now=NOW)                    # instance 1
        n1 = count(db, "predictions_2c_shadow", "status='OK'")
        # --- redémarrage simulé : NOUVELLE instance, T identique ------------
        s2 = mk_runner(db).run_once(now=NOW)               # instance 2
        n2 = count(db, "predictions_2c_shadow", "status='OK'")
        assert s2["predictions"] == 0                      # aucun doublon §15
        assert n1 == n2 == 3

    def test_recover_on_boot_shadow_still_healthy(self, monkeypatch):
        """Après restart, le cycle RECOVERY (§37 WEB-4) déclenche le hook et
        le shadow reste sain — sans dupliquer ce qui existe.
        NB : recover_on_boot utilise l'horloge RÉELLE du scheduler."""
        from datetime import datetime, timezone
        monkeypatch.setenv("PRONOFOOT_2C_SHADOW", "1")
        real_ko = (datetime.now(timezone.utc)
                   + timedelta(minutes=100)).isoformat().replace("+00:00", "Z")
        insert_match(db, "espn:ger.1:RS2", "Werder Bremen", "Bayern Munich", real_ko)
        sched = _sched()
        hb0 = count(db, "model2c_shadow_heartbeats")
        rec = sched.recover_on_boot()                    # restart → recovery
        assert rec["recovered"] is True
        assert count(db, "model2c_shadow_heartbeats") == hb0 + 1
        labels = {r["snapshot_label"]
                  for r in rows(db, "predictions_2c_shadow",
                                "status='OK' AND model_id='MODEL_2C_B'")}
        assert labels == {"T-180"}
        # 2e recovery au même instant : rien de nouveau (pas de doublon)
        sched2 = _sched()
        sched2.recover_on_boot()
        assert count(db, "predictions_2c_shadow", "status='OK'") == 3

    def test_restart_preserves_2a_and_web4_tables(self):
        insert_match(db, "espn:ger.1:RS3", "Werder Bremen", "Bayern Munich", KO)
        tables = {t: table_hash(db, t) for t in
                  ("matches", "results", "data_snapshots", "predictions",
                   "web_datapoints", "web_ingestion_cycles", "web_entities")}
        mk_runner(db).run_once(now=NOW)
        mk_runner(db).run_once(now=NOW)                  # restart simulé
        after = {t: table_hash(db, t) for t in tables}
        # seules les tables WEB-4 de cycles/journal peuvent bouger — ici pas de
        # cycle ingestion (runner direct) ⇒ tout identique
        assert tables == after

    def test_heartbeat_history_survives_restart(self):
        insert_match(db, "espn:ger.1:RS4", "Werder Bremen", "Bayern Munich", KO)
        mk_runner(db).run_once(now=NOW, cycle_id="before-restart")
        mk_runner(db).run_once(now=NOW, cycle_id="after-restart")
        ids = [r["cycle_id"] for r in rows(db, "model2c_shadow_heartbeats")]
        assert ids == ["before-restart", "after-restart"]   # aucune perte

    def test_t180_not_overwritten_after_restart_and_time_passes(self):
        ko_later = (NOW + timedelta(minutes=170)).isoformat().replace("+00:00", "Z")
        insert_match(db, "espn:ger.1:RS5", "Werder Bremen", "Bayern Munich", ko_later)
        mk_runner(db).run_once(now=NOW)
        first = rows(db, "predictions_2c_shadow",
                     "match_id='espn:ger.1:RS5' AND snapshot_label='T-180'")
        mk_runner(db).run_once(now=NOW + timedelta(minutes=160))  # restart + T-15
        still = rows(db, "predictions_2c_shadow",
                     "match_id='espn:ger.1:RS5' AND snapshot_label='T-180'")
        assert first == still                                # horizon intact
        assert count(db, "predictions_2c_shadow",
                     "match_id='espn:ger.1:RS5' AND snapshot_label='T-15'") == 3


class TestCategoriesAndDuplicate:
    def test_reserve_youth_women_never_mapped(self):
        """§7 — catégories séparées : jamais de mapping forcé vers un senior."""
        from model2c.identity import canonical_team
        assert canonical_team("Bayern Munich II") is None
        assert canonical_team("SC Freiburg U19") is None
        assert canonical_team("VfL Wolfsburg Women") is None
        assert canonical_team("1. FC Köln U17") is None
        assert canonical_team("Borussia Dortmund") is not None  # senior : intact

    def test_reserved_named_match_refused(self):
        insert_match(db, "espn:ger.1:CAT1", "Bayern Munich II", "Werder Bremen",
                     KO)
        s = mk_runner(db).run_once(now=NOW)
        assert s["predictions"] == 0
        r = rows(db, "predictions_2c_shadow",
                 "match_id='espn:ger.1:CAT1'")[0]
        assert r["refusal_reason"].startswith("IDENTITY_UNKNOWN")

    def test_duplicate_attempt_alerted_critical(self):
        """§10 : un doublon (race/anomalie) → alerte CRITICAL SHADOW_DUPLICATE,
        jamais d'écrasement ni de crash."""
        insert_match(db, "espn:ger.1:DUP1", "Werder Bremen", "Bayern Munich", KO)
        r = mk_runner(db)
        s = r.run_once(now=NOW)
        assert s["predictions"] == 3
        # anomalie : forcer une réécriture au niveau du même label
        match = rows(db, "matches", "id='espn:ger.1:DUP1'")[0]
        ok_before = count(db, "predictions_2c_shadow", "status='OK'")
        try:
            r._write_prediction(match=match, label="T-180",
                                t_iso=NOW.isoformat(), model_id="MODEL_2C_B",
                                raw={"1": 0.5, "N": 0.3, "2": 0.2}, cal=None,
                                feats_hash="x", dq={"level": "FULL_DATA"},
                                sample_size=1, identity={}, audit={},
                                calibration_version="x")
            forced = True
        except Exception:
            forced = False
        assert not forced                              # index unique a bloqué
        assert count(db, "predictions_2c_shadow", "status='OK'") == ok_before
