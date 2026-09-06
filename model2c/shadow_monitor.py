# -*- coding: utf-8 -*-
"""
ÉTAPE 2C.1 — DIAGNOSTIC SHADOW INTERNE (§16)
=============================================
Compteurs exposés dans /api/ingestion/status (bloc additif « shadow2c ») :
JAMAIS de probabilités, JAMAIS de données match. Défensif : si la migration
v4 est absente, on le dit honnêtement (MIGRATION_MISSING) sans casser
l'endpoint.
"""

from .shadow_hook import enabled

_TABLES_OK_SQL = ("SELECT name FROM sqlite_master WHERE type='table' "
                  "AND name='predictions_2c_shadow'")


def _has_v4(db):
    try:
        if not db.query(_TABLES_OK_SQL, one=True):
            return False
        cols = {r["name"] for r in db.query("PRAGMA table_info(predictions_2c_shadow)")}
        return "snapshot_label" in cols
    except Exception:
        return False


def status_block(db_module):
    db = db_module
    out = {"enabled": enabled(),
           "shadow_predictions": None, "shadow_errors": None,
           "shadow_last_run": None, "shadow_last_success": None,
           "shadow_insufficient": None, "shadow_leakage_rejections": None,
           "shadow_identity_unknown": None, "comparator": None,
           "schema": None}
    if not _has_v4(db):
        out["schema"] = "MIGRATION_MISSING"
        return out
    out["schema"] = "v4"
    row = db.query(
        """SELECT COUNT(*) AS c,
                  SUM(CASE WHEN status='OK' THEN 1 ELSE 0 END) AS ok,
                  SUM(CASE WHEN status='NO_PREDICTION' THEN 1 ELSE 0 END) AS refused
           FROM predictions_2c_shadow""", one=True)
    out["shadow_predictions"] = int(row["ok"] or 0)
    out["shadow_errors"] = db.query(
        "SELECT COUNT(*) AS c FROM model2c_shadow_alerts WHERE level IN ('ERROR','CRITICAL')",
        one=True)["c"]
    for reason, key in (("INSUFFICIENT_DATA", "shadow_insufficient"),
                        ("REFUSE_PREDICTION", "shadow_leakage_rejections"),
                        ("IDENTITY_UNKNOWN", "shadow_identity_unknown")):
        out[key] = db.query(
            """SELECT COUNT(*) AS c FROM predictions_2c_shadow
               WHERE refusal_reason LIKE %s""", (reason + "%",), one=True)["c"]
    last = db.query(
        """SELECT at_utc, summary_json FROM model2c_shadow_heartbeats
           ORDER BY id DESC LIMIT 1""", one=True)
    if last:
        out["shadow_last_run"] = last["at_utc"]
    ok_last = db.query(
        """SELECT at_utc FROM model2c_shadow_heartbeats
           WHERE summary_json NOT LIKE '%SHADOW_%MISSING%'
           ORDER BY id DESC LIMIT 1""", one=True)
    out["shadow_last_success"] = ok_last["at_utc"] if ok_last else None
    # comparateur : N commun par label (compteurs seulement — §24)
    comp = {}
    for label in ("T-180", "T-60", "T-15"):
        n = db.query(
            """SELECT COUNT(DISTINCT s.id) AS c FROM predictions_2c_shadow s
               JOIN results r ON r.match_id = s.match_id
               JOIN predictions p ON p.match_id = s.match_id AND p.market='1N2'
               WHERE s.status='OK' AND s.snapshot_label=%s""",
            (label,), one=True)["c"]
        comp[label] = {"n_common": n,
                       "sample": ("INSUFFICIENT_SAMPLE" if n < 30 else
                                  "OBSERVATION_ONLY" if n < 100 else
                                  "COMPARISON_ELIGIBLE")}
    out["comparator"] = comp
    return out
