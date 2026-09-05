# -*- coding: utf-8 -*-
"""
TESTS WEB-4 — UNITAIRES (100 % hors-ligne)
===========================================
Un périmètre par section :
A. WebCacheSQLite (§3/§4)      — hit / miss / expiration / remplacement /
                                 checksum / restart / corruption / §38 secrets
B. PersistentPITStore (§5/§6)  — append-only / versionnage / as_of / doublons
C. PersistentResearchJournal   — persistance / whitelist anti-secrets
D. IngestionMetrics + alertes  — compteurs / seuils §29
E. data_quality_score (§20)    — formule WEB-0, jamais une proba de victoire
F. EntityStore (§17/§18)       — seuils WEB-1 / contradictions / catégories
G. VenueStore (§19)            — coordonnées obligatoires / ambiguïté ⇒ None
H. Bridge (§12–§16)            — candidats T-180/T-60/T-15, kickoff §15,
                                 insert-only, dédup, comparaison canary
I. Scheduler (unités)          — phases / due / config ligue / build_ctx
"""
import json
import sqlite3

import pytest

import db as db_layer
import repository as repo

from sources.web.normalized import UNKNOWN, new_datapoint, unknown_point
from sources.web.web4 import bridge, metrics as w4m
from sources.web.web4.config import WEB4_CONFIG
from sources.web.web4.entity_store import EntityStore, team_key
from sources.web.web4.journal_sqlite import PersistentResearchJournal
from sources.web.web4.persistent_cache import WebCacheSQLite, new_entry
from sources.web.web4.pit_sqlite import PersistentPITStore
from sources.web.web4.quality_score import data_quality_score
from sources.web.web4.scheduler import (IngestionScheduler, OL_SLUG,
                                        fd_season_code, ol_season)
from sources.web.web4.venues import VenueStore

R = "2026-09-05T10:00:00Z"          # retrieved_at de référence
T0 = "2026-09-05T10:00:00Z"
T1 = "2026-09-05T10:05:00Z"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    p = str(tmp_path / "t.db")
    db_layer.init(path=p, reset=True)
    yield p


def _dp(value=8, dt="shots", mid="m1", retrieved=R, source="espn",
        team_id=None, level="RAW", **kw):
    return new_datapoint(value, dt, source, retrieved, match_id=mid,
                         team_id=team_id, level=level, **kw)


# ===========================================================================
# A. CACHE PERSISTANT (§3/§4)
# ===========================================================================
class TestWebCache:
    def test_A01_miss_sur_cle_inconnue(self):
        assert WebCacheSQLite().get("espn|score_live|http://x") is None

    def test_A02_set_puis_hit_contenu_exact(self):
        c = WebCacheSQLite()
        e = new_entry(200, "application/json", b'{"a":1}', R)
        c.set("espn|score_live|http://x", e, 60)
        hit = c.get("espn|score_live|http://x")
        assert hit["body"] == b'{"a":1}' and hit["status"] == 200

    def test_A03_hit_conserve_retrieved_at_ORIGINAL(self):
        c = WebCacheSQLite()
        c.set("k|dt|u", new_entry(200, "application/json", b"1", R), 60)
        assert c.get("k|dt|u")["retrieved_at"] == R     # jamais ré-horodaté

    def test_A04_expiration_wallclock(self, tmp_path):
        t = {"now": 1000.0}
        c = WebCacheSQLite(clock=lambda: t["now"])
        c.set("k|dt|u", new_entry(200, "application/json", b"1", R), 10)
        assert c.get("k|dt|u") is not None
        t["now"] = 1011.0                               # TTL dépassé
        assert c.get("k|dt|u") is None

    def test_A05_expiree_supprimee_de_la_base(self):
        t = {"now": 1000.0}
        c = WebCacheSQLite(clock=lambda: t["now"])
        c.set("k|dt|u", new_entry(200, "application/json", b"1", R), 10)
        t["now"] = 2000.0
        c.get("k|dt|u")
        assert c.size() == 0

    def test_A06_ttl_none_jamais_expire(self):
        c = WebCacheSQLite()
        c.set("k|dt|u", new_entry(200, "application/json", b"1", R), None)
        assert c.get("k|dt|u") is not None

    def test_A07_remplacement_autorise_UNIQUEMENT_cache(self):
        c = WebCacheSQLite()
        c.set("k|dt|u", new_entry(200, "application/json", b"old", R), 60)
        c.set("k|dt|u", new_entry(200, "application/json", b"new", T1), 60)
        hit = c.get("k|dt|u")
        assert hit["body"] == b"new" and hit["retrieved_at"] == T1

    def test_A08_checksum_enregistre(self):
        c = WebCacheSQLite()
        c.set("k|dt|u", new_entry(200, "application/json", b"abc", R), 60)
        row = db_layer.query("SELECT checksum FROM web_cache", one=True)
        import hashlib
        assert row["checksum"] == hashlib.sha256(b"abc").hexdigest()

    def test_A09_corruption_detectee_et_purgee(self):
        c = WebCacheSQLite()
        c.set("k|dt|u", new_entry(200, "application/json", b"abc", R), 60)
        # corruption directe du BLOB (simulation disque)
        db_layer.execute(
            "UPDATE web_cache SET response_payload=%s WHERE cache_key=%s",
            (b"CORRUPTED", "k|dt|u"))
        assert c.get("k|dt|u") is None                  # miss, jamais douteux
        assert c.size() == 0                            # entrée purgée

    def test_A10_restart_persistence_meme_fichier(self, tmp_path):
        path = tmp_path / "t.db"
        c1 = WebCacheSQLite()
        c1.set("k|dt|u", new_entry(200, "application/json", b"abc", R), 60)
        # « redémarrage » : NOUVELLE instance sur le MÊME fichier
        c2 = WebCacheSQLite()
        hit = c2.get("k|dt|u")
        assert hit is not None and hit["body"] == b"abc"
        assert hit["retrieved_at"] == R

    def test_A11_url_jamais_stockee_en_clair(self):
        c = WebCacheSQLite()
        # le schéma ne connaît AUCUNE colonne « url » — seulement url_hash
        cols = [r[1] for r in db_layer.query("PRAGMA table_info(web_cache)")]
        assert "url" not in cols
        assert "url_hash" in cols
        # et une clé contenant un motif de secret est refusee (§38)
        secretish = "http://x/?api_key=SHOULD_NOT_BE_STORED"
        assert c.set(f"espn|score_live|{secretish}",
                     new_entry(200, "application/json", b"1", R), 60) is False
        assert c.size() == 0

    def test_A12_cle_avec_motif_interdit_refusee(self):
        c = WebCacheSQLite()
        assert c.set("x|dt|http://h/?token=abc",
                     new_entry(200, "application/json", b"1", R), 60) is False
        assert c.get("x|dt|http://h/?token=abc") is None
        assert c.size() == 0

    def test_A13_len_et_size(self):
        c = WebCacheSQLite()
        c.set("a|d|u", new_entry(200, "application/json", b"1", R), 60)
        c.set("b|d|u", new_entry(200, "application/json", b"2", R), 60)
        assert len(c) == 2 == c.size()

    def test_A14_purge_expired_ne_touche_pas_les_valides(self):
        t = {"now": 1000.0}
        c = WebCacheSQLite(clock=lambda: t["now"])
        c.set("old|d|u", new_entry(200, "application/json", b"1", R), 5)
        c.set("ok|d|u", new_entry(200, "application/json", b"2", R), 500)
        t["now"] = 1100.0
        assert c.purge_expired() == 1
        assert c.get("ok|d|u") is not None

    def test_A15_key_compat_memorycache(self):
        from sources.web.safe_http import _MemoryCache
        assert (WebCacheSQLite.key("espn", "http://u", "score_live") ==
                _MemoryCache.key("espn", "http://u", "score_live"))


# ===========================================================================
# B. PIT STORE PERSISTANT (§5/§6)
# ===========================================================================
class TestPitSqlite:
    def test_B01_add_puis_query(self):
        s = PersistentPITStore()
        s.add(_dp())
        pts = s.query(match_id="m1")
        assert len(pts) == 1 and pts[0].value == 8

    def test_B02_refuse_non_datapoint(self):
        with pytest.raises(TypeError):
            PersistentPITStore().add({"value": 1})

    def test_B03_versionnage_deux_instants_CONSERVES(self):
        """§6 — ESPN 14:00 → 8, 14:05 → 10 : les DEUX restent visibles."""
        s = PersistentPITStore()
        s.add(_dp(value=8, retrieved=R))
        s.add(_dp(value=10, retrieved=T1))
        assert s.count() == 2
        assert {p.value for p in s.query(data_type="shots")} == {8, 10}

    def test_B04_doublon_EXACT_ignore(self):
        s = PersistentPITStore()
        dp = _dp()
        s.add(dp)
        s.add(_dp())                                    # même valeur/même T
        assert s.count() == 1

    def test_B05_anti_leakage_as_of(self):
        s = PersistentPITStore()
        s.add(_dp(value=8, retrieved=R))
        s.add(_dp(value=10, retrieved=T1))              # futur à 10:04
        seen = s.query(match_id="m1", as_of="2026-09-05T10:04:00Z")
        assert len(seen) == 1 and seen[0].value == 8

    def test_B06_known_at_reconstruit_le_passe(self):
        s = PersistentPITStore()
        s.add(_dp(value=8, retrieved=R))
        s.add(_dp(value=10, retrieved=T1))
        assert [p.value for p in s.known_at("m1", "2026-09-05T10:04:59Z")] == [8]
        assert len(s.known_at("m1", T1)) == 2

    def test_B07_unknown_persiste_comme_unknown(self):
        s = PersistentPITStore()
        s.add(unknown_point("xg", "statsbomb_open", R, match_id="m1"))
        pts = s.query(match_id="m1", data_type="xg")
        assert len(pts) == 1 and pts[0].value is UNKNOWN

    def test_B08_filtres_source_et_team(self):
        s = PersistentPITStore()
        s.add(_dp(source="espn", team_id="H"))
        s.add(_dp(source="football_data_co_uk", team_id="A", value=5))
        assert len(s.query(source="espn")) == 1
        assert len(s.query(team_id="A")) == 1

    def test_B09_only_valid_par_defaut(self):
        s = PersistentPITStore()
        bad = _dp()
        bad.valid = False
        s.add(bad)
        assert s.query() == [] and len(s.query(only_valid=False)) == 1

    def test_B10_latest_par_retrieved_at(self):
        s = PersistentPITStore()
        s.add(_dp(value=8, retrieved=R))
        s.add(_dp(value=10, retrieved=T1))
        assert s.latest("m1", "shots", T1).value == 10

    def test_B11_restart_persistence(self):
        PersistentPITStore().add(_dp())
        s2 = PersistentPITStore()                       # « redémarrage »
        assert s2.count() == 1 and s2.query()[0].value == 8

    def test_B12_effective_at_futur_exclu(self):
        s = PersistentPITStore()
        dp = new_datapoint(1, "weather", "open_meteo", R,
                           effective_at="2026-09-06T00:00:00Z", match_id="m1")
        s.add(dp)
        assert s.query(as_of=T1) == []                  # efficace demain
        assert len(s.query(as_of="2026-09-06T00:00:01Z")) == 1

    def test_B13_derived_avec_provenance_complete(self):
        s = PersistentPITStore()
        dp = new_datapoint(0.65, "xg", "statsbomb_open", R, match_id="m1",
                           level="DERIVED",
                           derivation_method="sum_statsbomb_xg",
                           model_version="web3-v1", inputs=["e1", "e2"])
        s.add(dp)
        got = s.query(data_type="xg")[0]
        assert got.level == "DERIVED" and got.model_version == "web3-v1"
        assert got.inputs == ["e1", "e2"]

    def test_B14_dump_complet(self):
        s = PersistentPITStore()
        s.add(_dp())
        s.add(unknown_point("wind", "open_meteo", R, match_id="m1"))
        assert len(s.dump()) == 2


# ===========================================================================
# C. JOURNAL PERSISTANT (§23/§38)
# ===========================================================================
class TestJournalSqlite:
    def test_C01_record_et_lecture_persistante(self):
        j = PersistentResearchJournal()
        j.record(event="RESEARCH_OK", match_id="m1", source="espn",
                 status="OK", host="site.api.espn.com")
        evs = j.events(match_id="m1")
        assert len(evs) == 1 and evs[0]["source"] == "espn"

    def test_C02_cles_secretes_supprimees_a_l_enregistrement(self):
        j = PersistentResearchJournal()
        j.record(event="X", match_id="m1", token="SECRET", api_key="SECRET",
                 authorization="Bearer SECRET", cookie="SECRET")
        ev = j.events()[0]
        for k in ("token", "api_key", "authorization", "cookie"):
            assert k not in ev

    def test_C03_table_ne_contient_que_la_whitelist(self):
        j = PersistentResearchJournal()
        j.record(event="X", token="SECRET", surprise_field="SECRET",
                 match_id="m1")
        cols = [r[1] for r in db_layer.query(
            "PRAGMA table_info(web_research_events)")]
        for bad in ("token", "api_key", "cookie", "authorization",
                    "password", "secret", "surprise_field"):
            assert bad not in cols

    def test_C04_filtre_source(self):
        j = PersistentResearchJournal()
        j.record(event="E1", source="espn", match_id="m1")
        j.record(event="E2", source="open_meteo", match_id="m1")
        assert len(j.events(source="espn")) == 1

    def test_C05_count_persistant(self):
        j = PersistentResearchJournal()
        j.record(event="E", match_id="m1")
        assert j.count() == 1
        assert PersistentResearchJournal().count() == 1  # « restart »

    def test_C06_events_since(self):
        j = PersistentResearchJournal()
        j.record(event="OLD", match_id="m1", timestamp="2026-01-01T00:00:00Z")
        j.record(event="NEW", match_id="m1")
        since = j.events_since("2026-09-01T00:00:00Z")
        assert [e["event"] for e in since] == ["NEW"]

    def test_C07_memory_only_n_ecrit_pas(self):
        j = PersistentResearchJournal(memory_only=True)
        j.record(event="E", match_id="m1")
        assert j.count() == 1
        assert db_layer.query(
            "SELECT COUNT(*) AS c FROM web_research_events",
            one=True)["c"] == 0


# ===========================================================================
# D. MÉTRIQUES + ALERTES (§28/§29)
# ===========================================================================
class TestMetrics:
    def test_D01_incr_persiste(self):
        m = w4m.IngestionMetrics()
        m.incr("source_requests", 3)
        assert w4m.IngestionMetrics().get("source_requests") == 3.0

    def test_D02_cle_inconnue_refusee(self):
        with pytest.raises(ValueError):
            w4m.IngestionMetrics().incr("accuracy")     # métrique modèle !

    def test_D03_snapshot_et_duree_moyenne(self):
        m = w4m.IngestionMetrics()
        m.incr("ingestion_cycles")
        m.incr("cycle_duration_ms_total", 500)
        snap = m.snapshot()
        assert snap["average_cycle_duration_ms"] == 500.0
        assert "predictions_blocked" in snap

    def test_D04_alerte_warning_requests_over_budget(self):
        alerts = w4m.check_cycle_thresholds(
            {"requests_per_match": 9.0}, WEB4_CONFIG)
        assert alerts[0]["code"] == "REQUESTS_PER_MATCH_OVER_BUDGET"

    def test_D05_alerte_error_hard_cap(self):
        alerts = w4m.check_cycle_thresholds(
            {"requests_per_match": 21.0}, WEB4_CONFIG)
        assert alerts[0]["level"] == "ERROR"

    def test_D06_alerte_source_failure(self):
        alerts = w4m.check_cycle_thresholds(
            {"requests_per_match": 5, "source_failures_pct": 55.0},
            WEB4_CONFIG)
        assert any(a["code"] == "SOURCE_FAILURE_RATE_HIGH" for a in alerts)

    def test_D07_alerte_identity_unknown(self):
        alerts = w4m.check_cycle_thresholds(
            {"requests_per_match": 5, "identity_unknown_pct": 40.0},
            WEB4_CONFIG)
        assert any(a["code"] == "IDENTITY_UNKNOWN_RATE_HIGH" for a in alerts)

    def test_D08_critical_snapshot_after_kickoff(self):
        alerts = w4m.check_cycle_thresholds(
            {"requests_per_match": 1,
             "snapshot_after_kickoff": {"match_id": "m1"}}, WEB4_CONFIG)
        critical = [a for a in alerts if a["level"] == "CRITICAL"]
        assert critical and critical[0]["code"] == "SNAPSHOT_AFTER_KICKOFF"

    def test_D09_critical_prediction_modified(self):
        alerts = w4m.check_cycle_thresholds(
            {"requests_per_match": 1,
             "prediction_modified": {"pid": "x"}}, WEB4_CONFIG)
        assert any(a["level"] == "CRITICAL" and
                   a["code"] == "PREDICTION_MODIFIED_AFTER_FREEZE"
                   for a in alerts)

    def test_D10_alertes_persistees_et_recentes(self):
        w4m.raise_alert("CRITICAL", "HASH_CHANGED", {"x": 1})
        rows = w4m.recent_alerts(limit=5)
        assert rows[0]["code"] == "HASH_CHANGED"
        assert rows[0]["level"] == "CRITICAL"


# ===========================================================================
# E. QUALITY SCORE (§20)
# ===========================================================================
class TestQualityScore:
    def test_E01_poids_somment_a_1(self):
        w = WEB4_CONFIG["quality_weights"]
        assert abs(sum(w.values()) - 1.0) < 1e-9

    def test_E02_label_anti_confusion(self):
        qs = data_quality_score([], {"sources": {}}, as_of=T0)
        assert "PAS une probabilité" in qs["label"]

    def test_E03_vide_score_zero(self):
        qs = data_quality_score([], {"sources": {}}, as_of=T0)
        assert qs["score"] == 0.0 and qs["grade"] == "D"

    def test_E04_unknown_degrade_le_score(self):
        pts = [_dp(), unknown_point("xg", "statsbomb_open", R, match_id="m1")]
        reg = {"sources": {"espn": {"reliability": 0.95},
                           "statsbomb_open": {"reliability": 0.9}}}
        qs = data_quality_score(pts, reg, as_of=R)
        assert qs["points_present"] == 1
        assert qs["components"]["completeness"] == 0.5

    def test_E05_confirmation_croisee(self):
        pts = [_dp(source="espn"), _dp(value=8, source="openligadb")]
        qs = data_quality_score(pts, {"sources": {}}, as_of=R)
        assert qs["components"]["confirmation"] == 1.0   # 2 sources confirment

    def test_E06_score_borne_0_1(self):
        pts = [_dp(), _dp(value=9, source="football_data_co_uk")]
        reg = {"sources": {"espn": {"reliability": 1.0},
                           "football_data_co_uk": {"reliability": 1.0}}}
        qs = data_quality_score(pts, reg, as_of=R,
                                expected_types=["shots"])
        assert 0.0 <= qs["score"] <= 1.0

    def test_E07_temporal_zero_si_point_futur(self):
        dp = new_datapoint(1, "weather", "open_meteo", "2026-09-06T00:00:00Z",
                           match_id="m1")
        qs = data_quality_score([dp], {"sources": {}}, as_of=T0)
        assert qs["components"]["temporal_validity"] == 0.0


# ===========================================================================
# F. ENTITY STORE (§17/§18)
# ===========================================================================
class TestEntityStore:
    def test_F01_auto_stocke(self):
        s = EntityStore()
        d = s.propose("team", "psg|open", "espn", "983",
                      confidence=0.96)
        assert d["stored"] is True and d["status"] == "MATCH_AUTO"

    def test_F02_journalized_stocke_marque(self):
        s = EntityStore()
        d = s.propose("team", "psg|open", "espn", "983", confidence=0.85)
        assert d["stored"] is True and d["status"] == "MATCH_JOURNALIZED"

    def test_F03_unknown_jamais_stocke(self):
        s = EntityStore()
        d = s.propose("team", "psg|open", "espn", "983", confidence=0.5)
        assert d["stored"] is False and s.count() == 0

    def test_F04_contradiction_refusee_et_alertee(self):
        s = EntityStore()
        s.propose("team", "psg|open", "espn", "983", confidence=0.99)
        d = s.propose("team", "psg|open", "espn", "999", confidence=0.99)
        assert d["status"] == "CONTRADICTION" and d["stored"] is False
        rows = db_layer.query(
            "SELECT * FROM web_alerts WHERE code='ENTITY_CONTRADICTION'")
        assert len(list(rows)) == 1
        assert s.resolve("team", "espn", "psg|open") == "983"  # intact

    def test_F05_resolve_et_reverse(self):
        s = EntityStore()
        s.propose("team", "newcastle|open", "espn", "361", confidence=0.97)
        assert s.resolve("team", "espn", "newcastle|open") == "361"
        assert s.reverse("team", "espn", "361") == "newcastle|open"

    def test_F06_resolve_source_inconnue_none(self):
        assert EntityStore().resolve("team", "fotmob", "x") is None

    def test_F07_categories_distinctes_femmes(self):
        """§18 — PSG féminin ≠ PSG masculin (jamais la même clé)."""
        k_m = team_key("Paris Saint-Germain")
        k_f = team_key("Paris Saint-Germain Women")
        assert k_m != k_f

    def test_F08_categories_distinctes_jeunes(self):
        k_a = team_key("Ajax")
        k_u19 = team_key("Ajax U19")
        assert k_a != k_u19

    def test_F09_propose_team_helper(self):
        s = EntityStore()
        s.propose_team("Newcastle United", "espn", "361", confidence=0.96)
        assert s.resolve_team("Newcastle United", "espn") == "361"

    def test_F10_export_map_format_entity_map(self):
        s = EntityStore()
        s.propose("team", "newcastle|open", "espn", "361", confidence=0.97)
        exp = s.export_map()
        assert "team_ids" in exp and exp["team_ids"]["newcastle|open"]
        ent = exp["team_ids"]["newcastle|open"]["espn"]
        assert ent["external_id"] == "361" and ent["status"] == "MATCH_AUTO"

    def test_F11_champs_manquants_refuses(self):
        d = EntityStore().propose("team", "", "espn", "1", confidence=0.99)
        assert d["stored"] is False

    def test_F12_entity_type_invalide(self):
        with pytest.raises(ValueError):
            EntityStore().propose("alien", "k", "espn", "1", confidence=0.99)


# ===========================================================================
# G. VENUE STORE (§19)
# ===========================================================================
class TestVenueStore:
    def test_G01_add_avec_coordonnees(self):
        v = VenueStore()
        vid = v.add("St James' Park", city="Newcastle", country="England",
                    latitude=54.9756, longitude=-1.6217, source="wikidata")
        assert vid and v.get(vid)["latitude"] == pytest.approx(54.9756)

    def test_G02_refus_sans_coordonnees(self):
        with pytest.raises(ValueError):
            VenueStore().add("Stade Mystère", source="wikidata")

    def test_G03_refus_coordonnees_hors_plage(self):
        with pytest.raises(ValueError):
            VenueStore().add("S", latitude=91.0, longitude=0.0, source="w")

    def test_G04_refus_sans_source(self):
        with pytest.raises(ValueError):
            VenueStore().add("S", latitude=1.0, longitude=1.0, source=None)

    def test_G05_find_exact_puis_coords(self):
        v = VenueStore()
        v.add("St James' Park", city="Newcastle", country="England",
              latitude=54.9756, longitude=-1.6217, source="wikidata")
        coords = v.coords_for("St James' Park", city="Newcastle",
                              country="England")
        assert coords == (54.9756, -1.6217)

    def test_G06_ambiguite_retourne_none(self):
        v = VenueStore()
        v.add("Stadium Alpha", latitude=1.0, longitude=1.0, source="w")
        v.add("Stadium Beta", latitude=2.0, longitude=2.0, source="w")
        assert v.find("Stadium") is None              # 2 candidats ⇒ None

    def test_G07_inconnu_retourne_none(self):
        assert VenueStore().find("Nowhere") is None

    def test_G08_meteo_sans_stade_prouve_reste_unknown(self):
        """Conséquence directe §19 : aucun stade ⇒ aucune coordonnée ⇒
        l'adapter météo ne construira AUCUNE requête (WEB-3)."""
        assert VenueStore().coords_for("Stade Inconnu") is None

    def test_G09_filtre_pays_leve_ambiguite(self):
        v = VenueStore()
        v.add("Olympic Stadium", city="London", country="England",
              latitude=51.5, longitude=-0.02, source="w")
        v.add("Olympic Stadium", city="Montreal", country="Canada",
              latitude=45.5, longitude=-73.5, source="w")
        exact = v.find("Olympic Stadium", city="London", country="England")
        assert exact["latitude"] == 51.5


# ===========================================================================
# H. BRIDGE (§12–§16) — candidats insert-only + kickoff protection
# ===========================================================================
KICK = "2026-09-05T20:00:00Z"
NOW_OK = "2026-09-05T16:59:00Z"      # T-181 min  → pas de candidat (fenêtre)
NOW_T180 = "2026-09-05T17:00:00Z"    # T-180 pile
NOW_T15 = "2026-09-05T19:50:00Z"


class TestBridge:
    def _store_with_point(self):
        repo.upsert_match({"id": "m1", "source": "espn", "source_match_id": "1",
                           "home_team": "H", "away_team": "A",
                           "kickoff_time_utc": KICK})
        s = PersistentPITStore()
        s.add(_dp(value=8, retrieved=R, mid="m1"))
        return s

    def test_H00_candidat_match_inconnu_refuse(self):
        s = PersistentPITStore()
        s.add(_dp(value=8, retrieved=R, mid="ghost"))
        r = bridge.create_snapshot_candidate(
            s, "ghost", KICK, "T-180", as_of=NOW_T180, now=NOW_T180,
            repository=repo)
        assert r["created"] is False and r["reason"] == "MATCH_UNKNOWN"

    def test_H01_candidat_cree_insert_only(self):
        s = self._store_with_point()
        r = bridge.create_snapshot_candidate(
            s, "m1", KICK, "T-180", as_of=NOW_T180, now=NOW_T180,
            repository=repo)
        assert r["created"] is True
        row = db_layer.query(
            "SELECT * FROM data_snapshots WHERE match_id='m1'", one=True)
        assert row["source"] == "web4-candidate"

    def test_H02_candidat_ne_cree_AUCUNE_prediction(self):
        s = self._store_with_point()
        bridge.create_snapshot_candidate(s, "m1", KICK, "T-180",
                                         as_of=NOW_T180, now=NOW_T180,
                                         repository=repo)
        assert db_layer.table_counts()["predictions"] == 0

    def test_H03_dedup_contenu_identique(self):
        s = self._store_with_point()
        args = dict(as_of=NOW_T180, now=NOW_T180, repository=repo)
        r1 = bridge.create_snapshot_candidate(s, "m1", KICK, "T-180", **args)
        r2 = bridge.create_snapshot_candidate(s, "m1", KICK, "T-180", **args)
        assert r1["created"] and r2["reason"] == "IDENTICAL"
        n = db_layer.query(
            "SELECT COUNT(*) AS c FROM data_snapshots WHERE match_id='m1' "
            "AND source='web4-candidate'", one=True)["c"]
        assert n == 1

    def test_H04_kickoff_passe_REFUS_et_CRITICAL(self):
        s = self._store_with_point()
        r = bridge.create_snapshot_candidate(
            s, "m1", KICK, "T-15", as_of="2026-09-05T20:01:00Z",
            now="2026-09-05T20:01:00Z", repository=repo)
        assert r["created"] is False and r["reason"] == "KICKOFF_PASSED"
        alerts = db_layer.query(
            "SELECT * FROM web_alerts WHERE code='SNAPSHOT_AFTER_KICKOFF'")
        assert len(list(alerts)) == 1
        assert db_layer.query(
            "SELECT COUNT(*) AS c FROM data_snapshots WHERE source"
            "='web4-candidate'", one=True)["c"] == 0

    def test_H05_fenetre_invalide(self):
        with pytest.raises(ValueError):
            bridge.create_snapshot_candidate(
                self._store_with_point(), "m1", KICK, "T-42",
                as_of=NOW_T180, now=NOW_T180, repository=repo)

    def test_H06_anti_leakage_dans_payload(self):
        repo.upsert_match({"id": "m1", "source": "espn", "source_match_id": "1",
                           "home_team": "H", "away_team": "A",
                           "kickoff_time_utc": KICK})
        s = PersistentPITStore()
        s.add(_dp(value=8, retrieved=R, mid="m1"))
        s.add(_dp(value=99, retrieved="2026-09-05T17:30:00Z", mid="m1"))
        r = bridge.create_snapshot_candidate(
            s, "m1", KICK, "T-180", as_of=NOW_T180, now=NOW_T180,
            repository=repo)
        payload = db_layer.query(
            "SELECT payload_json FROM data_snapshots WHERE id=%s",
            (r["snapshot_id"],), one=True)["payload_json"]
        assert '"value":99' not in payload               # point futur exclu
        assert '"value":8' in payload

    def test_H07_t_window_taggee(self):
        s = self._store_with_point()
        r = bridge.create_snapshot_candidate(
            s, "m1", KICK, "T-60", as_of="2026-09-05T19:00:00Z",
            now="2026-09-05T19:00:00Z", repository=repo)
        payload = json.loads(db_layer.query(
            "SELECT payload_json FROM data_snapshots WHERE id=%s",
            (r["snapshot_id"],), one=True)["payload_json"])
        assert payload["t_window"] == "T-60"
        assert payload["candidate"] is True

    def test_H08_compare_canary_structure(self):
        repo.upsert_match({"id": "m1", "source": "espn", "source_match_id": "1",
                           "home_team": "H", "away_team": "A",
                           "kickoff_time_utc": KICK})
        repo.insert_snapshot("m1", NOW_T180, "espn", {"x": 1}, "full",
                             ["team_stats"])
        s = self._store_with_point()
        bridge.create_snapshot_candidate(
            s, "m1", KICK, "T-180", as_of=NOW_T180, now=NOW_T180,
            repository=repo)
        cmp_ = bridge.compare_with_2a("m1")
        assert cmp_["snapshot_2a"]["fields"] == ["team_stats"]
        assert "shots" in cmp_["candidate_web4"]["fields"]
        assert cmp_["diff"]["fields_only_in_web4"] == ["shots"]

    def test_H09_integrity_scan_detecte_corruption(self):
        repo.upsert_match({"id": "m1", "source": "espn", "source_match_id": "1",
                           "home_team": "H", "away_team": "A",
                           "kickoff_time_utc": KICK})
        snap = repo.insert_snapshot("m1", NOW_OK, "espn", {"x": 1},
                                    "full", ["team_stats"])
        # corruption directe du snapshot (aucune prédiction réfère : cas pur)
        db_layer.execute(
            "UPDATE data_snapshots SET payload_json='{\"x\":2}' WHERE id=%s",
            (snap["id"],))
        import prediction_service as predsvc
        out = bridge.integrity_scan(limit=10, predsvc=predsvc)
        assert out["checked"] == 0                        # aucune prédiction
        # avec une prédiction référençant ce snapshot corrompu :
        from datetime import datetime, timezone
        frozen_at = datetime.now(timezone.utc).isoformat()
        repo.insert_prediction_version("m1", [{
            "id": "p1", "version_seq": 1, "frozen_at": frozen_at,
            "kickoff_time_utc": KICK, "market": "1N2", "selection": "1",
            "raw_probability": 0.5, "published_probability": 0.5,
            "fair_odds": 2.0, "bookmaker_odds": None,
            "distribution_json": json.dumps({"1": .5, "N": .3, "2": .2}),
            "model_name": "poisson", "model_version": "1.0.0",
            "snapshot_id": snap["id"], "snapshot_hash": snap["hash"],
            "prediction_hash": "X" * 64}])
        out = bridge.integrity_scan(limit=10, predsvc=predsvc)
        assert out["corrupted"] == ["p1"]
        alerts = db_layer.query("SELECT * FROM web_alerts WHERE code="
                                "'HASH_CHANGED'")
        assert len(list(alerts)) == 1

    def test_H10_candidats_listes_par_match(self):
        s = self._store_with_point()
        for w, ts in (("T-180", NOW_T180), ("T-60", "2026-09-05T19:00:00Z"),
                      ("T-15", NOW_T15)):
            bridge.create_snapshot_candidate(s, "m1", KICK, w, as_of=ts,
                                             now=ts, repository=repo)
        cands = bridge.candidates_for_match("m1")
        assert len(cands) == 3                            # v1/v2/v3 conservées


# ===========================================================================
# I. SCHEDULER — unités (§7/§11)
# ===========================================================================
from datetime import datetime, timedelta, timezone   # noqa: E402

UTC = timezone.utc
SNOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _sched(store=None, journal=None, entities=None, venues=None):
    store = store or PersistentPITStore()
    journal = journal or PersistentResearchJournal()
    factory = lambda: None                              # pas utilisé ici
    return IngestionScheduler(factory, store, journal,
                              metrics=w4m.IngestionMetrics(),
                              entity_store=entities or EntityStore(),
                              venue_store=venues or VenueStore(),
                              clock=lambda: SNOW)


class TestSchedulerUnits:
    def test_I01_phase_horizon(self):
        s = _sched()
        ko = (SNOW + timedelta(minutes=181)).isoformat()
        assert s.phase_for(ko) == "HORIZON"

    def test_I02_phase_t180_borne(self):
        s = _sched()
        ko = (SNOW + timedelta(minutes=180)).isoformat()
        assert s.phase_for(ko) == "T-180"

    def test_I03_phase_t60(self):
        s = _sched()
        ko = (SNOW + timedelta(minutes=59)).isoformat()
        assert s.phase_for(ko) == "T-60"

    def test_I04_phase_t15(self):
        s = _sched()
        ko = (SNOW + timedelta(minutes=10)).isoformat()
        assert s.phase_for(ko) == "T-15"

    def test_I05_phase_live_puis_post(self):
        s = _sched()
        ko_live = (SNOW - timedelta(minutes=30)).isoformat()
        ko_post = (SNOW - timedelta(minutes=151)).isoformat()
        assert s.phase_for(ko_live) == "LIVE"
        assert s.phase_for(ko_post) == "POST"

    def test_I06_due_sans_historique(self):
        assert _sched().due("calendar") is True

    def test_I07_due_respecte_frequence(self):
        s = _sched()
        s._state_set("last_cycle:T-15", SNOW.isoformat())
        assert s.due("T-15") is False
        s._state_set("last_cycle:T-15",
                     (SNOW - timedelta(minutes=6)).isoformat())
        assert s.due("T-15") is True            # fréquence 5 min dépassée

    def test_I08_fd_season_code(self):
        assert fd_season_code(datetime(2026, 9, 5)) == "2627"
        assert fd_season_code(datetime(2027, 2, 1)) == "2627"

    def test_I09_ol_season(self):
        assert ol_season(datetime(2026, 9, 5)) == "2026"
        assert ol_season(datetime(2027, 3, 1)) == "2026"

    def test_I10_build_ctx_ligues_mappees(self):
        s = _sched()
        row = {"id": "m1", "source": "espn", "source_match_id": "42",
               "competition": "eng.1", "home_team": "H FC", "away_team": "A FC",
               "kickoff_time_utc": (SNOW + timedelta(minutes=170)).isoformat(),
               "venue": None}
        ctx = s.build_ctx(row)
        assert ctx["fd_league"] == "E0" and ctx["fd_season"] == "2627"
        assert "ol_slug" not in ctx                      # pas allemand
        assert ctx["event_id"] == "42"

    def test_I11_build_ctx_openligadb_seulement_allemand(self):
        s = _sched()
        row = {"id": "m2", "source": "espn", "source_match_id": "7",
               "competition": "ger.1", "home_team": "H", "away_team": "A",
               "kickoff_time_utc": (SNOW + timedelta(minutes=170)).isoformat(),
               "venue": None}
        ctx = s.build_ctx(row)
        assert ctx["ol_slug"] == OL_SLUG["ger.1"] == "bl1"

    def test_I12_build_ctx_meteo_sans_stade_prouve(self):
        s = _sched()
        row = {"id": "m3", "source": "espn", "source_match_id": "8",
               "competition": "eng.1", "home_team": "H", "away_team": "A",
               "kickoff_time_utc": (SNOW + timedelta(minutes=170)).isoformat(),
               "venue": "Stade Non Référencé"}
        ctx = s.build_ctx(row)
        assert "lat" not in ctx and "lon" not in ctx     # §19

    def test_I13_build_ctx_meteo_avec_stade_prouve(self):
        vs = VenueStore()
        vs.add("St James' Park", city="Newcastle", country="England",
               latitude=54.9756, longitude=-1.6217, source="wikidata")
        s = _sched(venues=vs)
        row = {"id": "m4", "source": "espn", "source_match_id": "9",
               "competition": "eng.1", "home_team": "H", "away_team": "A",
               "kickoff_time_utc": (SNOW + timedelta(minutes=170)).isoformat(),
               "venue": "St James' Park"}
        ctx = s.build_ctx(row)
        assert ctx["lat"] == 54.9756

    def test_I14_select_upcoming_dans_horizon(self):
        repo.upsert_match({"id": "mA", "source": "espn", "source_match_id": "1",
                           "home_team": "H", "away_team": "A",
                           "kickoff_time_utc": (SNOW + timedelta(hours=5))
                           .isoformat()})
        repo.upsert_match({"id": "mB", "source": "espn", "source_match_id": "2",
                           "home_team": "C", "away_team": "D",
                           "kickoff_time_utc": (SNOW + timedelta(days=9))
                           .isoformat()})
        ids = [r["id"] for r in _sched().select_upcoming()]
        assert "mA" in ids and "mB" not in ids           # 9 jours > horizon 48 h

    def test_I15_config_canary_bornee(self):
        assert WEB4_CONFIG["matches_per_cycle"] <= 10    # §36 niveau 1
        assert WEB4_CONFIG["requests_per_match_budget"] == 8
        assert WEB4_CONFIG["requests_per_match_hard_cap"] == 20
