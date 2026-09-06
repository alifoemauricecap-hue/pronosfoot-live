# -*- coding: utf-8 -*-
"""2C.1 §6-§15/§18-§21 — runner shadow : éligibilité, labels, identité,
anti-leakage, calibration, immutabilité, reproductibilité, non-régression 2A."""
import json
from datetime import timedelta

import pytest
import db

from helpers_2c1 import (NOW, fresh_db, restore_db, insert_match, insert_result,
                         insert_2a_1n2, runner as mk_runner, count, rows,
                         table_hash)


@pytest.fixture(autouse=True)
def _db(tmp_path):
    prev = fresh_db(tmp_path)
    yield
    restore_db(prev)


UP = lambda m: (NOW + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")


def _bayern_match(mid="espn:ger.1:M1", when=100, home="Werder Bremen",
                  away="Bayern Munich"):
    return insert_match(db, mid, home, away, UP(when))


class TestEligibilityAndLabels:
    def test_ok_predictions_written_b_c_d(self):
        _bayern_match()
        s = mk_runner(db).run_once(now=NOW, cycle_id="t")
        assert s["eligible"] == 1 and s["predictions"] == 3 and not s["errors"]
        got = {r["model_id"] for r in rows(db, "predictions_2c_shadow",
                                           "status='OK'")}
        assert got == {"MODEL_2C_B", "MODEL_2C_C", "MODEL_2C_D"}

    def test_label_t180_t60_t15(self):
        insert_match(db, "espn:ger.1:L1", "Werder Bremen", "Bayern Munich", UP(100))
        insert_match(db, "espn:ger.1:L2", "Werder Bremen", "Bayern Munich", UP(40))
        insert_match(db, "espn:ger.1:L3", "Werder Bremen", "Bayern Munich", UP(10))
        mk_runner(db).run_once(now=NOW)
        labels = {(r["match_id"], r["snapshot_label"])
                  for r in rows(db, "predictions_2c_shadow",
                                "status='OK' AND model_id='MODEL_2C_B'")}
        assert labels == {("espn:ger.1:L1", "T-180"), ("espn:ger.1:L2", "T-60"),
                          ("espn:ger.1:L3", "T-15")}

    def test_beyond_t180_not_eligible(self):
        _bayern_match("espn:ger.1:F1", when=300)
        s = mk_runner(db).run_once(now=NOW)
        assert s["eligible"] == 0 and count(db, "predictions_2c_shadow") == 0

    def test_non_german_competitions_ignored(self):
        insert_match(db, "espn:eng.1:X1", "Werder Bremen", "Bayern Munich",
                     UP(80), competition="eng.1")
        s = mk_runner(db).run_once(now=NOW)
        assert s["eligible"] == 0

    def test_canary_max_5(self):
        for i in range(8):
            insert_match(db, f"espn:ger.1:C{i}", "Werder Bremen", "Bayern Munich",
                         UP(60 + i))
        s = mk_runner(db).run_once(now=NOW)
        assert s["eligible"] == 5

    def test_t180_never_replaced_by_t15(self):
        mid = _bayern_match("espn:ger.1:R1", when=170)
        r1 = mk_runner(db).run_once(now=NOW)
        first = rows(db, "predictions_2c_shadow",
                     "match_id=? AND snapshot_label='T-180'", (mid,))
        later = NOW + timedelta(minutes=160)     # le même match passe à T-15
        r2 = mk_runner(db).run_once(now=later)
        row180_after = rows(db, "predictions_2c_shadow",
                            "match_id=? AND snapshot_label='T-180'", (mid,))
        assert first == row180_after                # T-180 INTACT (append-only)
        labels = {r["snapshot_label"]
                  for r in rows(db, "predictions_2c_shadow",
                                "match_id=? AND status='OK' AND model_id='MODEL_2C_B'",
                                (mid,))}
        assert labels == {"T-180", "T-15"}

    def test_dedup_same_label_once(self):
        _bayern_match()
        mk_runner(db).run_once(now=NOW)
        s2 = mk_runner(db).run_once(now=NOW)
        assert s2["predictions"] == 0 and s2["skipped_dedup"] == 1
        assert count(db, "predictions_2c_shadow", "status='OK'") == 3


class TestKickoffProtection:
    def test_passed_kickoff_not_eligible(self):
        insert_match(db, "espn:ger.1:P1", "Werder Bremen", "Bayern Munich",
                     (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"))
        s = mk_runner(db).run_once(now=NOW)
        assert s["eligible"] == 0
        assert count(db, "predictions_2c_shadow") == 0

    def test_kickoff_passed_branch_refuses_with_alert(self):
        """Défense §14 : si le kickoff est passé entre sélection et calcul →
        KICKOFF_PASSED + alerte CRITICAL, jamais de prédiction ex-post."""
        m = _bayern_match("espn:ger.1:P2", when=30)
        r = mk_runner(db)
        match = rows(db, "matches", "id=?", (m,))[0]
        r._predict_one(match, NOW + timedelta(minutes=31),
                       (NOW + timedelta(minutes=31)).isoformat(), None,
                       elo=None, strengths=None, histories={}, seed_set=set(),
                       calib=None, calibration_version="x",
                       audit={"max_feature_effective_at": None,
                              "max_feature_retrieved_at": None},
                       hist=[], summary={"eligible": 1, "predictions": 0,
                                         "skipped_dedup": 0, "refusals": {},
                                         "errors": [], "alerts": []},
                       timings={"feature_ms": 0, "model_ms": 0, "db_ms": 0})
        ref = rows(db, "predictions_2c_shadow",
                   "match_id=? AND status='NO_PREDICTION'", (m,))
        assert ref and ref[0]["refusal_reason"] == "KICKOFF_PASSED"
        assert all(x["raw_home"] is None for x in ref)
        assert rows(db, "model2c_shadow_alerts", "code='KICKOFF_PASSED'")


class TestIdentity:
    def test_exact_identity_confidence_1(self):
        _bayern_match(away="bayern munchen")   # forme canonique exacte
        mk_runner(db).run_once(now=NOW)
        r = rows(db, "predictions_2c_shadow", "status='OK' LIMIT 1")[0]
        assert r["identity_confidence"] == 1.0
        assert r["identity_method"] == "EXACT"
        assert r["canonical_match_id"] == r["match_id"]
        assert r["source_match_id"] == "M1"

    def test_alias_identity_confidence_096(self):
        _bayern_match()                        # « Bayern Munich » = alias déclaré
        mk_runner(db).run_once(now=NOW)
        r = rows(db, "predictions_2c_shadow", "status='OK' LIMIT 1")[0]
        assert r["identity_confidence"] == 0.96
        assert r["identity_method"] == "ALIAS_DECLARED"

    def test_unmapped_name_refused_not_forced(self):
        insert_match(db, "espn:ger.2:U1", "Fortuna Koln",   # ambiguïté DÉCLARÉE (alias → None)
                     "Bayern Munich", UP(50), competition="ger.2")
        s = mk_runner(db).run_once(now=NOW)
        assert s["predictions"] == 0
        r = rows(db, "predictions_2c_shadow", "match_id='espn:ger.2:U1'")[0]
        assert r["status"] == "NO_PREDICTION"
        assert r["refusal_reason"] == "IDENTITY_UNKNOWN:UNMAPPED_NAME"
        assert r["raw_probabilities"] == "{}"

    def test_no_seed_history_refused(self):
        insert_match(db, "espn:ger.2:U2", "Energie Cottbus", "Hertha Berlin",
                     UP(50), competition="ger.2")
        mk_runner(db).run_once(now=NOW)
        r = rows(db, "predictions_2c_shadow", "match_id='espn:ger.2:U2'")[0]
        assert r["refusal_reason"] == "IDENTITY_UNKNOWN:NO_SEED_HISTORY"


class TestInsufficientAndUnknown:
    def test_insufficient_data_no_prediction_no_fake_probs(self):
        # équipe jamais vue par le seed synth (bl2 name)
        insert_match(db, "espn:ger.1:I1", "greuther furth", "Bayern Munich",
                     UP(50))
        mk_runner(db).run_once(now=NOW)
        r = rows(db, "predictions_2c_shadow", "match_id='espn:ger.1:I1'")[0]
        assert r["status"] == "NO_PREDICTION"
        assert r["refusal_reason"] in ("INSUFFICIENT_DATA",
                                       "IDENTITY_UNKNOWN:NO_SEED_HISTORY")
        assert r["raw_home"] is None and r["calibrated_home"] is None

    def test_unknown_never_zero(self):
        """UNKNOWN ≠ 0 : aucune colonne de proba à 0.0 sur une ligne OK, et
        les features xG absentes restent NULL (jamais 0)."""
        _bayern_match("espn:ger.1:Z1")
        mk_runner(db).run_once(now=NOW)
        for r in rows(db, "predictions_2c_shadow", "status='OK'"):
            for col in ("raw_home", "raw_draw", "raw_away", "calibrated_home",
                        "calibrated_draw", "calibrated_away"):
                assert r[col] is not None and 0.0 < r[col] < 1.0
        fv = rows(db, "features_2c", "match_id=?", ("espn:ger.1:Z1",))[0]
        vec = json.loads(fv["feature_json"])
        assert vec.get("home_xg_for_rate") is None      # UNKNOWN — pas 0

    def test_data_level_recorded(self):
        _bayern_match("espn:ger.1:DL1")
        mk_runner(db).run_once(now=NOW)
        r = rows(db, "predictions_2c_shadow",
                 "status='OK' AND match_id='espn:ger.1:DL1' LIMIT 1")[0]
        assert r["data_level"] in ("FULL_DATA", "PARTIAL_DATA", "LOW_DATA")
        assert r["sample_size"] > 0


class TestAntiLeak:
    def test_settlement_never_changes_past_prediction(self):
        """§21 : un résultat réglé APRÈS une prédiction ne la modifie JAMAIS
        et ne figurait pas dans ses features (preuve par hash)."""
        mid = _bayern_match("espn:ger.1:S1", when=100)
        mk_runner(db).run_once(now=NOW)
        before = rows(db, "predictions_2c_shadow", "match_id=?", (mid,))
        fbefore = rows(db, "features_2c", "match_id=?", (mid,))
        # --- règlement ultérieur (comme le ferait 2A) ---
        db.execute("UPDATE matches SET status='FINISHED', home_score=2, away_score=1"
                   " WHERE id=?", (mid,))
        insert_result(db, mid, 2, 1,
                      captured_at=NOW + timedelta(hours=3))
        after = rows(db, "predictions_2c_shadow", "match_id=?", (mid,))
        fafter = rows(db, "features_2c", "match_id=?", (mid,))
        assert before == after               # prédiction gelée INTACTE
        assert fbefore == fafter             # vecteur de features INTACT
        assert fbefore[0]["as_of"] <= before[0]["prediction_time"]

    def test_future_result_not_synced_before_its_time(self):
        """Un résultat dont captured_at > T n'entre PAS dans l'état à T."""
        mid = insert_match(db, "espn:ger.1:S2", "Werder Bremen", "Bayern Munich",
                           (NOW - timedelta(hours=26)).isoformat().replace("+00:00", "Z"),
                           status="FINISHED", hg=5, ag=5)
        insert_result(db, mid, 5, 5, captured_at=NOW + timedelta(hours=1))
        s = mk_runner(db).run_once(now=NOW)
        assert count(db, "model2c_team_events") == 0     # pas encore visible
        assert s["team_events"]["new_results"] == 0

    def test_result_folded_once_visible(self):
        mid = insert_match(db, "espn:ger.1:S3", "Werder Bremen", "Bayern Munich",
                           (NOW - timedelta(hours=26)).isoformat().replace("+00:00", "Z"),
                           status="FINISHED", hg=1, ag=0)
        insert_result(db, mid, 1, 0, captured_at=NOW - timedelta(hours=25))
        s = mk_runner(db).run_once(now=NOW)
        assert s["team_events"]["new_results"] == 1
        ev = rows(db, "model2c_team_events", "match_id=?", (mid,))
        assert ev and ev[0]["home"] == "werder bremen" and ev[0]["hg"] == 1
        # 2e passage : pas de re-join (UNIQUE match_id)
        s2 = mk_runner(db).run_once(now=NOW)
        assert s2["team_events"]["new_results"] == 0

    def test_audit_fields_present_and_consistent(self):
        _bayern_match("espn:ger.1:A1")
        mk_runner(db).run_once(now=NOW)
        r = rows(db, "predictions_2c_shadow",
                 "status='OK' AND match_id='espn:ger.1:A1' LIMIT 1")[0]
        assert r["max_feature_effective_at"] is not None
        assert r["max_feature_effective_at"] <= r["prediction_time"]
        assert (r["max_feature_retrieved_at"] is None
                or r["max_feature_retrieved_at"] <= r["prediction_time"])

    def test_retrieved_leak_refused_critical(self):
        # événement visible mais capturé APRÈS T → fuite → refus + CRITICAL
        db.execute("INSERT INTO model2c_team_events (match_id, league, kickoff_utc,"
                   " home, away, hg, ag, captured_at, processed_at)"
                   " VALUES ('leak','bl1','2026-09-06T09:00:00+00:00','a','b',1,0,"
                   " '2026-09-06T23:59:00+00:00','2026-09-06T09:05:00+00:00')")
        _bayern_match("espn:ger.1:A2")
        s = mk_runner(db).run_once(now=NOW)
        ref = rows(db, "predictions_2c_shadow",
                   "match_id='espn:ger.1:A2' AND status='NO_PREDICTION'")[0]
        assert ref["refusal_reason"].startswith("REFUSE_PREDICTION:LEAK")
        assert rows(db, "model2c_shadow_alerts",
                    "code LIKE 'REFUSE_PREDICTION%' AND level='CRITICAL'")
        assert s["refusals"].get("REFUSE_PREDICTION", 0) >= 1

    def test_effective_leak_refused(self):
        """Une feature dont effective_at > T ⇒ REFUSE_PREDICTION + CRITICAL."""
        _bayern_match("espn:ger.1:A3")
        r = mk_runner(db)
        orig_hist = r._history
        future = (NOW + timedelta(hours=9)).isoformat()

        def leaky_history(now_dt):
            hist, _me, mr = orig_hist(now_dt)
            return hist, future, mr          # effective_at injecté DANS le futur

        r._history = leaky_history
        r.run_once(now=NOW)
        ref = rows(db, "predictions_2c_shadow",
                   "match_id='espn:ger.1:A3' AND status='NO_PREDICTION'")[0]
        assert ref["refusal_reason"] == "REFUSE_PREDICTION:LEAK_EFFECTIVE"
        assert rows(db, "model2c_shadow_alerts",
                    "code='REFUSE_PREDICTION:LEAK_EFFECTIVE' AND level='CRITICAL'")


class TestCalibrationAndVersioning:
    def test_raw_and_calibrated_kept_sums_one(self):
        _bayern_match("espn:ger.1:K1")
        mk_runner(db).run_once(now=NOW)
        for r in rows(db, "predictions_2c_shadow", "status='OK'"):
            assert abs(r["raw_home"] + r["raw_draw"] + r["raw_away"] - 1) < 5e-6
            assert abs(r["calibrated_home"] + r["calibrated_draw"]
                       + r["calibrated_away"] - 1) < 5e-6
            assert (r["raw_home"], r["raw_draw"]) != (r["calibrated_home"],
                                                      r["calibrated_draw"])

    def test_frozen_platt_formula_applied(self):
        """La calibration prod = la formule figée (a=1.05, b=-0.03 synth),
        RECALCULÉE à la main dans le test — jamais quelque chose d'opaque."""
        import math
        _bayern_match("espn:ger.1:K2")
        mk_runner(db).run_once(now=NOW)
        r = rows(db, "predictions_2c_shadow",
                 "status='OK' AND model_id='MODEL_2C_B'")[0]
        expected = {}
        for raw, key in ((r["raw_home"], "1"), (r["raw_draw"], "N"),
                         (r["raw_away"], "2")):
            z = math.log(raw / (1 - raw))
            expected[key] = 1 / (1 + math.exp(-(1.05 * z - 0.03)))
        tot = sum(expected.values())
        assert abs(r["calibrated_home"] - expected["1"] / tot) < 1e-5
        assert abs(r["calibrated_away"] - expected["2"] / tot) < 1e-5
        assert r["calibration_version"] == "2c-calib-synth-test"

    def test_model_version_and_hashes_recorded(self):
        from model2c.shadow_prod import SHADOW_MODEL_VERSION
        _bayern_match("espn:ger.1:V1")
        mk_runner(db).run_once(now=NOW)
        r = rows(db, "predictions_2c_shadow", "status='OK' LIMIT 1")[0]
        assert r["model_version"] == SHADOW_MODEL_VERSION
        assert len(r["features_hash"]) == 64
        assert len(r["dataset_hash"]) == 64
        assert len(r["code_hash"]) == 64
        assert len(r["shadow_hash"]) == 64
        assert r["feature_version"]

    def test_registry_versions_experimental(self):
        _bayern_match("espn:ger.1:V2")
        mk_runner(db).run_once(now=NOW)
        vers = rows(db, "model2c_versions")
        assert vers and all(v["status"] == "EXPERIMENTAL" for v in vers)
        assert {v["model_id"] for v in vers} >= {"MODEL_2C_B"}

    def test_immutability_prediction_update_delete(self):
        _bayern_match("espn:ger.1:IM1")
        mk_runner(db).run_once(now=NOW)
        with pytest.raises(Exception):
            db.execute("UPDATE predictions_2c_shadow SET raw_home=0.5")
        with pytest.raises(Exception):
            db.execute("DELETE FROM predictions_2c_shadow")

    def test_reproducibility_two_fresh_dbs_same_hash(self, tmp_path):
        _bayern_match("espn:ger.1:RZ")
        mk_runner(db).run_once(now=NOW)
        h1 = rows(db, "predictions_2c_shadow",
                  "status='OK' AND model_id='MODEL_2C_B'")[0]["features_hash"]
        p1 = rows(db, "predictions_2c_shadow",
                  "status='OK' AND model_id='MODEL_2C_B'")[0]["raw_probabilities"]
        restore_db(db.db_path())
        prev = fresh_db(tmp_path / "second")
        _bayern_match("espn:ger.1:RZ")
        mk_runner(db).run_once(now=NOW)
        h2 = rows(db, "predictions_2c_shadow",
                  "status='OK' AND model_id='MODEL_2C_B'")[0]["features_hash"]
        p2 = rows(db, "predictions_2c_shadow",
                  "status='OK' AND model_id='MODEL_2C_B'")[0]["raw_probabilities"]
        assert h1 == h2 and p1 == p2          # reproductible (§29)
        restore_db(prev)


class TestPerformance:
    def test_timings_recorded(self):
        _bayern_match("espn:ger.1:T1")
        s = mk_runner(db).run_once(now=NOW)
        t = s["timings"]
        for k in ("feature_ms", "model_ms", "db_ms", "total_ms"):
            assert k in t and isinstance(t[k], int) and t[k] >= 0

    def test_heartbeat_written(self):
        mk_runner(db).run_once(now=NOW, cycle_id="hb1")
        hb = rows(db, "model2c_shadow_heartbeats", "cycle_id='hb1'")
        assert hb and "timings" in json.loads(hb[0]["summary_json"])

    def test_no_direct_sources_or_network(self):
        """§5 : le runner ne touche JAMAIS SafeHttpClient ni les sources."""
        import model2c.shadow_prod as sp
        src = open(sp.__file__, encoding="utf-8").read()
        assert "SafeHttpClient" not in src and "requests." not in src
        assert "safe_http" not in src and "http" not in src.lower().replace(
            "https://", "")