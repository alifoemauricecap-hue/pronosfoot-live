# -*- coding: utf-8 -*-
"""
SHADOW PREDICTIONS 2C (§25)
============================
Table predictions_2c_shadow : APPEND-ONLY (trigger SQL no-update/no-delete).
INVISIBLE sur le frontend (aucune route, aucun affichage — §32).
Chaque ligne est reproductible : features_hash + dataset_hash + model_version
+ calibration_version (§29).
"""

import hashlib
import json
from datetime import datetime, timezone

import db
from .config2c import CALIBRATION_VERSION


def _sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=True, default=str).encode()).hexdigest()


def write_shadow(match_id, prediction_time, model_id, model_version,
                 raw_probabilities, calibrated_probabilities, features_hash,
                 dataset_hash, data_quality, sample_size,
                 code_hash, calibration_version=None, status="EXPERIMENTAL"):
    """Insert d'une prédiction shadow (jamais d'update)."""
    pid = _sha({"m": match_id, "t": prediction_time, "model": model_id,
                "v": model_version, "raw": raw_probabilities})[:32]
    row_hash = _sha({"pid": pid, "raw": raw_probabilities,
                     "cal": calibrated_probabilities, "fh": features_hash,
                     "dh": dataset_hash})
    db.execute("""INSERT INTO predictions_2c_shadow
        (id, match_id, prediction_time, model_version, model_id,
         raw_probabilities, calibrated_probabilities, features_hash,
         dataset_hash, data_quality_json, sample_size, calibration_version,
         code_hash, status, shadow_hash, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (pid, match_id, prediction_time, model_version, model_id,
         json.dumps(raw_probabilities, sort_keys=True),
         json.dumps(calibrated_probabilities, sort_keys=True) if calibrated_probabilities else None,
         features_hash, dataset_hash, json.dumps(data_quality or {}, sort_keys=True),
         int(sample_size or 0), calibration_version or CALIBRATION_VERSION,
         code_hash, status, row_hash, datetime.now(timezone.utc).isoformat()))
    return pid


def list_shadow(match_id=None, model_id=None, limit=1000):
    sql = "SELECT * FROM predictions_2c_shadow"
    cond, params = [], []
    if match_id:
        cond.append("match_id=%s"); params.append(match_id)
    if model_id:
        cond.append("model_id=%s"); params.append(model_id)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += f" ORDER BY created_at LIMIT {int(limit)}"
    return db.rows_to_dicts(db.query(sql, params))


def count_shadow():
    return db.query("SELECT COUNT(*) AS c FROM predictions_2c_shadow", one=True)["c"]
