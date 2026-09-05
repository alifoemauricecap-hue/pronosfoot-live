# -*- coding: utf-8 -*-
"""
MODEL REGISTRY 2C (§24/§29)
=============================
Chaque version : model_id, version, features, feature_version,
training_window, calibration_version, dataset_hash, code_hash, metrics,
created_at, status. Statuts : EXPERIMENTAL / VALIDATED / CANARY /
PRODUCTION / RETIRED. Toute cette étape reste EXPERIMENTAL.

Reproductibilité : dataset_hash (canonique) + code_hash (SHA-256 des
fichiers du package) + feature_hash (par vecteur) + model_version figée.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

import db
from .config2c import CONFIG_2C as C, FEATURE_VERSION, CALIBRATION_VERSION

STATUSES = ("EXPERIMENTAL", "VALIDATED", "CANARY", "PRODUCTION", "RETIRED")


def compute_code_hash(root=None):
    """SHA-256 du contenu des fichiers model2c/*.py triés — toute évolution du
    code change le hash (détection de non-reproductibilité)."""
    root = root or os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(root, name), "rb") as f:
            h.update(name.encode())
            h.update(f.read())
    return h.hexdigest()


def compute_dataset_hash(matches):
    """SHA-256 du dataset canonique (trié, sérialisation stable)."""
    canon = json.dumps(matches, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def register_model(model_id, version, metrics, training_window, dataset_hash,
                   code_hash, features=None, status=None, calibration_version=None):
    """Insert (idempotent par UNIQUE) d'une version de modèle 2C."""
    status = status or C["status"]
    if status not in STATUSES:
        raise ValueError(f"statut invalide : {status}")
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""INSERT OR IGNORE INTO model2c_versions
        (model_id, version, features_json, feature_version, training_window_json,
         calibration_version, dataset_hash, code_hash, metrics_json, created_at, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (model_id, version, json.dumps(features or {}, sort_keys=True), FEATURE_VERSION,
         json.dumps(training_window or {}, sort_keys=True),
         calibration_version or CALIBRATION_VERSION, dataset_hash, code_hash,
         json.dumps(metrics or {}, sort_keys=True), now, status))
    return {"model_id": model_id, "version": version, "status": status}


def get_models(status=None):
    rows = db.query("SELECT * FROM model2c_versions ORDER BY created_at")
    out = db.rows_to_dicts(rows)
    if status:
        out = [r for r in out if r["status"] == status]
    return out


def set_status(model_id, version, status):
    if status not in STATUSES:
        raise ValueError(f"statut invalide : {status}")
    db.execute("UPDATE model2c_versions SET status=%s WHERE model_id=%s AND version=%s",
               (status, model_id, version))
