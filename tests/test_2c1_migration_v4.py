# -*- coding: utf-8 -*-
"""2C.1 §3/§19 — migration v4 : additive, immuable, idempotente, 2A intacte."""
import sqlite3

import pytest
import db

from helpers_2c1 import fresh_db, restore_db, rows, table_hash


@pytest.fixture(autouse=True)
def _db(tmp_path):
    prev = fresh_db(tmp_path)
    yield
    restore_db(prev)


V4_COLS = {"snapshot_label", "refusal_reason", "raw_home", "raw_draw",
           "raw_away", "calibrated_home", "calibrated_draw", "calibrated_away",
           "max_feature_effective_at", "max_feature_retrieved_at",
           "source_match_id", "canonical_match_id", "identity_confidence",
           "identity_method", "data_level", "feature_version", "kickoff_time_utc"}

V4_TABLES = {"model2c_shadow_alerts", "model2c_shadow_heartbeats",
             "model2c_team_events", "model2c_dc_params"}


class TestMigrationV4:
    def test_version_4_applied(self):
        assert db.migration_version() >= 4

    def test_v4_columns_present(self):
        cols = {r["name"] for r in db.query("PRAGMA table_info(predictions_2c_shadow)")}
        assert V4_COLS <= cols

    def test_v4_tables_exist(self):
        names = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert V4_TABLES <= names

    def test_v3_v2_v1_tables_intact(self):
        names = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"matches", "results", "data_snapshots", "model_versions",
                "predictions", "prediction_results", "odds",
                "prediction_events", "cache_store",  # v1
                "web_cache", "web_datapoints", "web_research_events",
                "web_ingestion_cycles", "web_entities", "web_venues",
                "web_metrics", "web_scheduler_state", "web_alerts",  # v2
                "model2c_versions", "predictions_2c_shadow",
                "features_2c"} <= names                     # v3

    def test_2a_predictions_columns_unchanged(self):
        """Aucune colonne ajoutée à une table 2A (§3 : jamais de modif 2A)."""
        cols = {r["name"] for r in db.query("PRAGMA table_info(predictions)")}
        assert cols == {"id", "match_id", "version_seq", "prediction_created_at",
                        "prediction_effective_at", "kickoff_time_utc", "market",
                        "selection", "raw_probability", "published_probability",
                        "fair_odds", "bookmaker_odds", "distribution_json",
                        "model_name", "model_version", "snapshot_id",
                        "snapshot_hash", "prediction_hash", "prediction_status",
                        "frozen_at", "created_at"}

    def test_immutability_triggers_new_tables(self):
        now = db.utcnow()
        db.execute("INSERT INTO model2c_shadow_alerts (level, code, detail_json, at_utc)"
                   " VALUES ('WARNING','T','{}',?)", (now,))
        db.execute("INSERT INTO model2c_shadow_heartbeats (cycle_id, at_utc, duration_ms,"
                   " timings_json, summary_json) VALUES ('c',?,0,'{}','{}')", (now,))
        db.execute("INSERT INTO model2c_team_events (match_id, league, kickoff_utc,"
                   " home, away, hg, ag, captured_at, processed_at)"
                   " VALUES ('m','bl1','2026-09-01','h','a',1,0,?,?)", (now, now))
        db.execute("INSERT INTO model2c_dc_params (league, fit_day, params_json,"
                   " n_train, dataset_hash, created_at) VALUES ('bl1','2026-09-06',"
                   " '{}',10,'h',?)", (now,))
        for table in ("model2c_shadow_alerts", "model2c_shadow_heartbeats",
                      "model2c_team_events", "model2c_dc_params"):
            with pytest.raises(Exception):
                db.execute(f"UPDATE {table} SET id=id+1")
            with pytest.raises(Exception):
                db.execute(f"DELETE FROM {table}")

    def test_partial_unique_index_dedup_ok_only(self):
        """Une seule ligne OK par (match, modèle, version, label) ; les
        NO_PREDICTION et les autres labels restent possibles (§13)."""
        from model2c.shadow_prod import SHADOW_MODEL_VERSION
        base = dict(match_id="m1", prediction_time="2026-09-06T10:00:00+00:00",
                    model_version=SHADOW_MODEL_VERSION, model_id="MODEL_2C_B",
                    raw_probabilities="{}", calibrated_probabilities=None,
                    features_hash="f", dataset_hash="d", data_quality_json="{}",
                    sample_size=10, calibration_version="c", code_hash="x",
                    status="OK", shadow_hash="s1", created_at=db.utcnow(),
                    snapshot_label="T-60", refusal_reason=None)
        cols = ", ".join(base.keys())
        ph = ", ".join(["?"] * len(base))
        db.execute(f"INSERT INTO predictions_2c_shadow (id, {cols}) VALUES (?, {ph})",
                   ("p1", *base.values()))
        # même (match, modèle, label) en OK → refusé par l'index
        with pytest.raises(Exception):
            db.execute(f"INSERT INTO predictions_2c_shadow (id, {cols}) VALUES (?, {ph})",
                       ("p2", *{**base, "shadow_hash": "s2"}.values()))
        # autre label → autorisé (§13 : T-180 jamais remplacé par T-15)
        db.execute(f"INSERT INTO predictions_2c_shadow (id, {cols}) VALUES (?, {ph})",
                   ("p3", *{**base, "snapshot_label": "T-15", "shadow_hash": "s3"}.values()))
        # NO_PREDICTION ne bloque pas l'index
        db.execute(f"INSERT INTO predictions_2c_shadow (id, {cols}) VALUES (?, {ph})",
                   ("p4", *{**base, "status": "NO_PREDICTION",
                            "refusal_reason": "INSUFFICIENT_DATA",
                            "shadow_hash": "s4"}.values()))
        assert db.query("SELECT COUNT(*) c FROM predictions_2c_shadow", one=True)["c"] == 3

    def test_migration_idempotent(self):
        db.migrate()
        db.migrate()
        assert db.migration_version() >= 4

    def test_v4_is_additive_in_source(self):
        """Le texte source de SCHEMA_V4 ne contient ni DROP ni suppression."""
        import re
        src = open("db.py", encoding="utf-8").read()
        v4 = src[src.index("SCHEMA_V4 = ["):src.index("MIGRATIONS = [")]
        assert "DROP TABLE" not in v4 and "DROP TRIGGER" not in v4
        for stmt in re.findall(r'"(ALTER TABLE [^"]+)"', v4):
            assert "ADD COLUMN" in stmt
