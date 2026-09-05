# -*- coding: utf-8 -*-
"""
OBSERVABILITÉ + ALERTES INTERNES (2B.WEB-4 §28/§29)
====================================================
Compteurs PERSISTANTS (table web_metrics — restart-safe §23) de qualité de
DONNÉES exclusivement. Jamais de métriques du modèle ici (accuracy/Brier/
LogLoss/calibration appartiennent au socle 2A — separation of concerns).

ALERTES (§29) : WARNING / ERROR / CRITICAL dans web_alerts.
CRITICAL = protection du socle : snapshot après kickoff, prédiction modifiée
après gel, hash altéré. Jamais bloquant pour le LIVE.
"""
import json
import threading
import time

import db as _db

from ..normalized import now_iso

#: §28 — métriques produites (toutes de qualité de DONNÉES)
METRIC_KEYS = (
    "ingestion_cycles", "successful_cycles", "failed_cycles",
    "source_requests", "cache_hits", "cache_misses", "source_errors",
    "fallback_used", "unknown_data", "conflicts", "invalid_data",
    "identity_matches", "identity_unknown",
    "snapshot_candidates", "snapshots_created",
    "predictions_created", "predictions_blocked",
    "cycle_duration_ms_total",
)

WARNING, ERROR, CRITICAL = "WARNING", "ERROR", "CRITICAL"
LEVELS = (WARNING, ERROR, CRITICAL)


class IngestionMetrics:
    """Compteurs persistants. `mem` reflète la base au boot puis est
    incrémenté en miroir (lecture immédiate, sans SQL à chaud)."""

    def __init__(self, db_module=None):
        self.db = db_module or _db
        self._lock = threading.Lock()
        self._mem = {}
        try:
            for r in self.db.query("SELECT key, value FROM web_metrics"):
                self._mem[r["key"]] = float(r["value"])
        except Exception:
            pass                        # base pas encore migrée (boot) : mémoire

    def incr(self, key, n=1):
        if key not in METRIC_KEYS:
            raise ValueError(f"métrique inconnue : {key}")
        with self._lock:
            self._mem[key] = self._mem.get(key, 0.0) + float(n)
            v = self._mem[key]
        self.db.execute(
            """INSERT INTO web_metrics (key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at""", (key, v, self.db.utcnow()))
        return v

    def get(self, key):
        with self._lock:
            return self._mem.get(key, 0.0)

    def snapshot(self):
        with self._lock:
            out = {k: self._mem.get(k, 0.0) for k in METRIC_KEYS}
        cyc = out["ingestion_cycles"] or 0
        out["average_cycle_duration_ms"] = (
            round(out["cycle_duration_ms_total"] / cyc, 1) if cyc else 0.0)
        return out


# ---------------------------------------------------------------------------
# ALERTES (§29)
# ---------------------------------------------------------------------------
def raise_alert(level, code, detail=None, db_module=None):
    """Persiste une alerte interne. CRITICAL est aussi affiché au log
    serveur. Jamais d'exception vers l'appelant (l'alerte ne casse rien)."""
    dbm = db_module or _db
    if level not in LEVELS:
        level = WARNING
    try:
        dbm.execute(
            "INSERT INTO web_alerts (level, code, detail_json, at_utc) "
            "VALUES (?,?,?,?)",
            (level, code,
             json.dumps(detail, ensure_ascii=False, default=str)
             if detail is not None else None, now_iso()))
        if level == CRITICAL:
            print(f"[web4][ALERTE CRITIQUE] {code} :: "
                  f"{json.dumps(detail, ensure_ascii=False, default=str)[:200]}")
    except Exception as e:
        print(f"[web4] alerte non persistée ({type(e).__name__})")
    return {"level": level, "code": code}


def recent_alerts(limit=20, level=None, db_module=None):
    dbm = db_module or _db
    sql = "SELECT * FROM web_alerts"
    params = []
    if level:
        sql += " WHERE level=%s"; params.append(level)
    sql += " ORDER BY id DESC LIMIT %s"; params.append(int(limit))
    return dbm.rows_to_dicts(dbm.query(sql, params))


# ---------------------------------------------------------------------------
# §29 — Évaluation des seuils sur les stats d'un cycle terminé
# ---------------------------------------------------------------------------
def check_cycle_thresholds(cycle_stats, config, db_module=None):
    """cycle_stats = {requests, requests_per_match, source_failures_pct,
    identity_unknown_pct, snapshot_after_kickoff, prediction_modified,
    hash_changed}. Émet les alertes prévues ; retourne la liste des alertes.
    """
    alerted = []
    acfg = config["alerts"]
    rpm = cycle_stats.get("requests_per_match", 0.0)
    if rpm > acfg["requests_per_match_error"]:
        alerted.append(raise_alert(
            ERROR, "REQUESTS_PER_MATCH_HARD_CAP",
            {"requests_per_match": rpm}, db_module))
    elif rpm > acfg["requests_per_match_warn"]:
        alerted.append(raise_alert(
            WARNING, "REQUESTS_PER_MATCH_OVER_BUDGET",
            {"requests_per_match": rpm, "objectif": 8}, db_module))
    sfp = cycle_stats.get("source_failures_pct", 0.0)
    if sfp > acfg["source_failure_warn_pct"]:
        alerted.append(raise_alert(
            WARNING, "SOURCE_FAILURE_RATE_HIGH",
            {"failures_pct": round(sfp, 1)}, db_module))
    iup = cycle_stats.get("identity_unknown_pct", 0.0)
    if iup > acfg["identity_unknown_warn_pct"]:
        alerted.append(raise_alert(
            WARNING, "IDENTITY_UNKNOWN_RATE_HIGH",
            {"identity_unknown_pct": round(iup, 1)}, db_module))
    # CRITICAL — protections du socle (§29)
    if cycle_stats.get("snapshot_after_kickoff"):
        alerted.append(raise_alert(
            CRITICAL, "SNAPSHOT_AFTER_KICKOFF",
            cycle_stats["snapshot_after_kickoff"], db_module))
    if cycle_stats.get("prediction_modified"):
        alerted.append(raise_alert(
            CRITICAL, "PREDICTION_MODIFIED_AFTER_FREEZE",
            cycle_stats["prediction_modified"], db_module))
    if cycle_stats.get("hash_changed"):
        alerted.append(raise_alert(
            CRITICAL, "HASH_CHANGED",
            cycle_stats["hash_changed"], db_module))
    return alerted


def monotonic_ms():
    return int(time.monotonic() * 1000)
