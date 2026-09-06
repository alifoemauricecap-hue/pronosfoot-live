# -*- coding: utf-8 -*-
"""2C.1 §15/§16/§20/§24 — comparateur 2A/2C (read-only, N≥30), monitoring,
non-régression 2A (hashes identiques), test de bout en bout sur ASSETS RÉELS."""
import json
from datetime import timedelta

import pytest
import db

from helpers_2c1 import (NOW, fresh_db, restore_db, insert_match, insert_result,
                         insert_2a_1n2, runner as mk_runner, count, rows,
                         table_hash, synth_seed)


@pytest.fixture(autouse=True)
def _db(tmp_path):
    prev = fresh_db(tmp_path)
    yield
    restore_db(prev)


UP = lambda m: (NOW + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")


def _full_cycle_with_settlement(mid="espn:ger.1:CP1"):
    """Prédiction shadow OK + prédiction 2A 1N2 + résultat → comparateur."""
    ko = UP(100)
    insert_match(db, mid, "Werder Bremen", "Bayern Munich", ko)
    mk_runner(db).run_once(now=NOW)
    insert_2a_1n2(db, mid, version_seq=1, probs=(0.30, 0.25, 0.45),
                  kickoff_iso=ko)
    db.execute("UPDATE matches SET status='FINISHED', home_score=1, away_score=2"
               " WHERE id=?", (mid,))
    insert_result(db, mid, 1, 2)
    return mid


class TestComparator:
    def test_n_below_30_insufficient_sample_no_metrics(self):
        _full_cycle_with_settlement()
        comp = mk_runner(db).compare_with_2a()
        assert comp["T-180"]["n"] == 1
        assert comp["T-180"]["verdict"] == "INSUFFICIENT_SAMPLE"
        assert "brier_2c_calibrated" not in comp["T-180"]   # jamais de métrique sur 1

    def test_comparator_is_read_only_on_2a(self):
        _full_cycle_with_settlement()
        before = {t: table_hash(db, t) for t in
                  ("predictions", "data_snapshots", "prediction_results",
                   "results", "matches")}
        mk_runner(db).compare_with_2a()
        after = {t: table_hash(db, t) for t in
                 ("predictions", "data_snapshots", "prediction_results",
                  "results", "matches")}
        assert before == after

    def test_n30_metrics_computed(self):
        for i in range(30):
            _full_cycle_with_settlement(f"espn:ger.1:M{i}")
        comp = mk_runner(db).compare_with_2a()
        e = comp["T-180"]
        assert e["verdict"] == "OBSERVATION_ONLY" and e["n"] == 30
        assert 0 <= e["brier_2c_calibrated"] <= 2 and 0 <= e["brier_2a"] <= 2
        assert e["logloss_2c_calibrated"] > 0 and e["logloss_2a"] > 0
        assert "brier_2c_raw" in e and "ece_2c_calibrated" in e
        assert 0 <= e["ece_2c_calibrated"] <= 1 and 0 <= e["ece_2a"] <= 1


class TestMonitor:
    def test_status_block_fields(self, monkeypatch):
        monkeypatch.delenv("PRONOFOOT_2C_SHADOW", raising=False)
        from model2c import shadow_monitor as sm
        out = sm.status_block(db)
        for k in ("enabled", "shadow_predictions", "shadow_errors",
                  "shadow_last_run", "shadow_last_success",
                  "shadow_insufficient", "shadow_leakage_rejections",
                  "shadow_identity_unknown", "comparator"):
            assert k in out
        assert out["enabled"] is False
        assert out["shadow_predictions"] == 0
        assert out["comparator"]["T-60"]["sample"] == "INSUFFICIENT_SAMPLE"

    def test_status_block_counts_after_run(self, monkeypatch):
        monkeypatch.setenv("PRONOFOOT_2C_SHADOW", "1")
        insert_match(db, "espn:ger.1:MM1", "Werder Bremen", "Bayern Munich", UP(50))
        insert_match(db, "espn:ger.2:MM2", "Energie Cottbus", "Hertha Berlin",
                     UP(50), competition="ger.2")
        from model2c import shadow_monitor as sm
        mk_runner(db).run_once(now=NOW, cycle_id="mon1")
        out = sm.status_block(db)
        assert out["enabled"] is True
        assert out["shadow_predictions"] == 3
        assert out["shadow_identity_unknown"] == 1
        assert out["shadow_last_run"] is not None
        assert out["schema"] == "v4"

    def test_status_block_migration_missing_graceful(self, tmp_path):
        """DB limitée à v2 (comme la prod AVANT déploiement) : bloc honnête."""
        import sqlite3 as s3
        p = str(tmp_path / "v2only.db")
        con = s3.connect(p)
        con.execute("CREATE TABLE matches (id TEXT)")
        con.execute("CREATE TABLE results (match_id TEXT)")
        con.execute("CREATE TABLE predictions (match_id TEXT, market TEXT)")
        con.commit()
        con.close()

        class MiniDB:
            def query(self, sql, params=(), one=False):
                con = s3.connect(p)
                con.row_factory = s3.Row
                try:
                    cur = con.execute(sql, params)
                    res = [dict(r) for r in cur.fetchall()]
                finally:
                    con.close()
                return (res[0] if res else None) if one else res
        from model2c import shadow_monitor as sm
        out = sm.status_block(MiniDB())
        assert out["schema"] == "MIGRATION_MISSING"
        assert out["shadow_predictions"] is None

    def test_ingestion_status_endpoint_has_block_without_break(self):
        """§16 : /api/ingestion/status étendu, jamais cassé, jamais de probas."""
        from sources.web.web4 import diagnostics as w4d
        out = w4d.ingestion_status()
        assert out["ok"] is True
        assert "shadow2c" in out
        blob = json.dumps(out["shadow2c"])
        for forbidden in ("raw_home", "calibrated_home", "probabilities"):
            assert forbidden not in blob


class TestNonRegression2A:
    def test_2a_tables_hash_identical_across_shadow_run(self):
        """§20 : le shadow ne modifie AUCUNE table 2A (preuve par hash)."""
        ko = UP(100)
        insert_match(db, "espn:ger.1:NR1", "Werder Bremen", "Bayern Munich", ko)
        insert_2a_1n2(db, "espn:ger.1:NR1", kickoff_iso=ko)
        insert_match(db, "espn:ger.1:NR2", "RB Leipzig", "Borussia Dortmund",
                     (NOW - timedelta(hours=30)).isoformat().replace("+00:00", "Z"),
                     status="FINISHED", hg=2, ag=2)
        insert_result(db, "espn:ger.1:NR2", 2, 2,
                      captured_at=NOW - timedelta(hours=28))
        tables_2a = ("matches", "results", "data_snapshots", "predictions",
                     "prediction_results", "model_versions", "odds",
                     "prediction_events", "cache_store", "web_cache",
                     "web_datapoints", "web_research_events",
                     "web_ingestion_cycles", "web_entities", "web_venues",
                     "web_metrics", "web_scheduler_state", "web_alerts")
        before = {t: table_hash(db, t) for t in tables_2a}
        s = mk_runner(db).run_once(now=NOW, cycle_id="nr")
        assert s["predictions"] > 0 or s["refusals"]
        after = {t: table_hash(db, t) for t in tables_2a}
        assert before == after


class TestRealAssetsEndToEnd:
    """Le SEUL test sur les VRAIS assets (seed OL 6120 matchs réels + Platt
    figé réel) — lent (~3 s) mais irremplaçable : c'est ce qui tournera en prod."""
    def test_real_seed_real_calibration_bcd_ok(self):
        from model2c import shadow_assets as assets
        if not assets.available():
            pytest.skip("assets prod absents")
        insert_match(db, "espn:ger.1:REAL1", "Werder Bremen", "Bayern Munich",
                     UP(100))
        from model2c.shadow_prod import ShadowRunner
        r = ShadowRunner(db_module=db, store=None, max_matches=5)
        s = r.run_once(now=NOW, cycle_id="real")
        assert not s["errors"]
        got = {x["model_id"]: x for x in rows(db, "predictions_2c_shadow",
                                              "status='OK'")}
        assert set(got) == {"MODEL_2C_B", "MODEL_2C_C", "MODEL_2C_D"}
        for x in got.values():
            assert abs(x["raw_home"] + x["raw_draw"] + x["raw_away"] - 1) < 5e-6
            assert x["calibration_version"] == "2c-calib-prod-1.0"
            assert x["data_level"] == "FULL_DATA"
        assert got["MODEL_2C_B"]["raw_probabilities"] != \
            got["MODEL_2C_B"]["calibrated_probabilities"]
        # DC quotidien mis en cache (bl1, n_train > 3000 réels)
        dc = rows(db, "model2c_dc_params", "league='bl1'")[0]
        assert dc["n_train"] > 3000
        # asset hash cohérent avec le manifeste réel
        assert got["MODEL_2C_B"]["dataset_hash"] == assets.dataset_hash()
