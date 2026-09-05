# -*- coding: utf-8 -*-
"""2C §24/§25/§29/§4 — migration v3, registry, shadow append-only, intégrité 2A."""
import json
import sqlite3
import pytest

import db
import repository as repo
from model2c import registry2c, shadow
from model2c.config2c import CONFIG_2C as C


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path):
    prev = db.db_path()
    db.init(str(tmp_path / "t.db"), reset=True)
    yield
    db.init(prev, reset=True)   # restaure le contexte global pour les autres suites


def _pin(name, version="1.0+t"):
    return registry2c.register_model(name, version, {"brier": 0.5}, {"leagues": ["bl1"]},
                                     "d" * 64, "c" * 64, status="EXPERIMENTAL")


class TestMigrationV3:
    def test_version_3_applied(self):
        assert db.migration_version() == 3

    def test_v3_tables_exist(self):
        names = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"model2c_versions", "predictions_2c_shadow", "features_2c"} <= names

    def test_v1_v2_tables_intact(self):
        names = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        v1 = {"matches", "results", "data_snapshots", "model_versions",
              "predictions", "prediction_results", "odds", "prediction_events",
              "cache_store"}
        v2 = {"web_cache", "web_datapoints", "web_research_events",
              "web_ingestion_cycles", "web_entities", "web_venues",
              "web_metrics", "web_scheduler_state", "web_alerts"}
        assert v1 <= names and v2 <= names

    def test_2a_immutability_trigger_still_active(self):
        repo.upsert_match({"id": "m1", "source": "test", "source_match_id": "1",
                           "competition": "x", "season": "s", "home_team": "H",
                           "away_team": "A", "kickoff_time_utc": "2026-01-01T00:00Z"})
        snap = repo.insert_snapshot("m1", "2026-01-01T00:00Z", "test",
                                    {"a": 1}, "full", ["team_stats"])
        repo.insert_prediction_version("m1", [{
            "id": "p1", "version_seq": 1, "kickoff_time_utc": "2026-01-01T00:00Z",
            "market": "1N2", "selection": "1", "raw_probability": 0.5,
            "published_probability": 0.5, "fair_odds": 2.0,
            "distribution_json": "{}", "model_name": "poisson",
            "model_version": "1.0.0", "snapshot_id": snap["id"],
            "snapshot_hash": snap["hash"], "prediction_hash": "x" * 64,
            "frozen_at": "2026-01-01T00:00Z"}])
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE predictions SET published_probability=0.9 WHERE id=%s", ("p1",))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM predictions WHERE id=%s", ("p1",))


class TestRegistry:
    def test_register_and_get(self):
        _pin("MODEL_2C_B")
        models = registry2c.get_models()
        assert len(models) == 1
        m = models[0]
        assert m["model_id"] == "MODEL_2C_B"
        assert m["status"] == "EXPERIMENTAL"
        assert m["dataset_hash"] == "d" * 64

    def test_status_lifecycle(self):
        _pin("MODEL_2C_B")
        registry2c.set_status("MODEL_2C_B", "1.0+t", "VALIDATED")
        assert registry2c.get_models(status="VALIDATED")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError):
            registry2c.register_model("X", "1", {}, {}, "d" * 64, "c" * 64,
                                      status="SUPER_AI")

    def test_idempotent_unique(self):
        _pin("MODEL_2C_B")
        _pin("MODEL_2C_B")  # INSERT OR IGNORE → pas de doublon
        assert len(registry2c.get_models()) == 1

    def test_code_hash_stable(self):
        h1 = registry2c.compute_code_hash()
        h2 = registry2c.compute_code_hash()
        assert h1 == h2 and len(h1) == 64

    def test_dataset_hash_stable_and_sensitive(self):
        a = registry2c.compute_dataset_hash([{"x": 1}])
        b = registry2c.compute_dataset_hash([{"x": 1}])
        c2 = registry2c.compute_dataset_hash([{"x": 2}])
        assert a == b and a != c2


class TestShadow:
    def _write(self):
        return shadow.write_shadow(
            "ol:bl1:1", "2024-08-23T18:30:00+00:00", "MODEL_2C_B",
            "MODEL_2C_B/1.0+2C.0-experimental",
            {"1": 0.5, "N": 0.3, "2": 0.2}, None,
            "f" * 64, "d" * 64, {"level": "FULL_DATA", "score": 0.9},
            22, "e" * 64)

    def test_write_and_read(self):
        pid = self._write()
        rows = shadow.list_shadow("ol:bl1:1")
        assert len(rows) == 1 and rows[0]["id"] == pid
        assert rows[0]["status"] == "EXPERIMENTAL"
        d = json.loads(rows[0]["raw_probabilities"])
        assert d == {"1": 0.5, "N": 0.3, "2": 0.2}

    def test_append_only_update_blocked(self):
        pid = self._write()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE predictions_2c_shadow SET status='PRODUCTION' WHERE id=%s", (pid,))

    def test_append_only_delete_blocked(self):
        pid = self._write()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM predictions_2c_shadow WHERE id=%s", (pid,))

    def test_features_delete_blocked(self):
        db.execute("""INSERT INTO features_2c (match_id, team_id, feature_name,
                      feature_value, feature_json, as_of, source, snapshot_id,
                      feature_version, feature_hash, created_at)
                      VALUES ('m','t','f',1.0,'{}','2026-01-01T00:00Z','x',NULL,'v','h','c')""")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM features_2c")

    def test_fields_spec_compliance(self):
        """§25 : tous les champs minimum exigés sont présents."""
        self._write()
        r = shadow.list_shadow("ol:bl1:1")[0]
        for field in ("match_id", "prediction_time", "model_version",
                      "raw_probabilities", "features_hash", "dataset_hash",
                      "data_quality_json", "sample_size", "calibration_version",
                      "status"):
            assert field in r and r[field] is not None

    def test_count(self):
        self._write()
        assert shadow.count_shadow() == 1

    def test_shadow_hash_verify(self):
        self._write()
        r = shadow.list_shadow("ol:bl1:1")[0]
        assert len(r["shadow_hash"]) == 64
