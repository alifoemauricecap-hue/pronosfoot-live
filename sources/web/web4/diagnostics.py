# -*- coding: utf-8 -*-
"""
DIAGNOSTIC D'INGESTION (2B.WEB-4 §30/§31)
==========================================
GET /api/ingestion/status — état PUBLIC de l'ingestion. Règles :
- JAMAIS de secrets : pas de token, pas d'URL avec clé, pas de contenu ;
- sources listées par ID + host de base (registry), jamais d'URL complète ;
- un problème secondaire d'une source ne rend PAS le cœur DOWN :
  CORE_HEALTH (app 2A) et DATA_HEALTH (ingestion) sont séparés (§31).
"""
from urllib.parse import urlsplit

import db as _db

from . import metrics as _metrics

_RUNTIME = {"scheduler": None, "client": None, "cache": None}


def set_runtime(scheduler=None, client=None, cache=None):
    """Enregistre les composants vivants (appelé par app.start_web4())."""
    _RUNTIME["scheduler"] = scheduler
    _RUNTIME["client"] = client
    _RUNTIME["cache"] = cache


def _host_of(base_url):
    try:
        return urlsplit(base_url or "").hostname
    except Exception:
        return None


def data_health(db_module=None):
    """DATA_HEALTH : ok / degraded / down — fondé sur les derniers cycles,
    indépendant du cœur applicatif (§31)."""
    dbm = db_module or _db
    try:
        last = dbm.query(
            """SELECT status, finished_at FROM web_ingestion_cycles
               WHERE finished_at IS NOT NULL
               ORDER BY finished_at DESC LIMIT 1""", one=True)
    except Exception:
        return "unknown"
    if not last:
        return "unknown"                       # jamais encore tourné
    if last["status"] == "DONE":
        return "ok"
    if last["status"] == "DONE_WITH_ERRORS":
        return "degraded"
    return "degraded"


def last_successful_cycle(db_module=None):
    dbm = db_module or _db
    r = dbm.query(
        """SELECT cycle_id, finished_at FROM web_ingestion_cycles
           WHERE status='DONE' AND finished_at IS NOT NULL
           ORDER BY finished_at DESC LIMIT 1""", one=True)
    return dict(r) if r else None


def ingestion_status(registry=None, db_module=None):
    """Corps de GET /api/ingestion/status. Jamais d'exception vers Flask."""
    dbm = db_module or _db
    out = {"ok": True, "core_health": "ok", "data_health": "unknown",
           "scheduler": {"enabled": _RUNTIME["scheduler"] is not None},
           "cache": {}, "cycles": {}, "sources": {}, "snapshots": {},
           "alerts": []}
    try:
        out["data_health"] = data_health(dbm)
        # ---- dernier cycle + prochain (estimé depuis l'état persistant)
        last = dbm.query(
            """SELECT cycle_id, trigger, phase, started_at, finished_at,
                      match_count, request_count, cache_hits, status
               FROM web_ingestion_cycles ORDER BY started_at DESC LIMIT 1""",
            one=True)
        out["cycles"]["last"] = dict(last) if last else None
        ok = last_successful_cycle(dbm)
        out["cycles"]["last_successful"] = ok
        n = dbm.query("SELECT COUNT(*) AS c FROM web_ingestion_cycles",
                      one=True)["c"]
        out["cycles"]["total"] = n
        agg = dbm.query(
            """SELECT COALESCE(SUM(match_count),0) AS m,
                      COALESCE(SUM(request_count),0) AS r
               FROM web_ingestion_cycles""", one=True)
        out["cycles"]["matches_processed"] = agg["m"]
        out["cycles"]["requests_total"] = agg["r"]
        # ---- cache persistant
        out["cache"]["persistent_entries"] = dbm.query(
            "SELECT COUNT(*) AS c FROM web_cache", one=True)["c"]
        if _RUNTIME["cache"] is not None:
            try:
                out["cache"]["backend_size"] = len(_RUNTIME["cache"])
            except Exception:
                pass
        # ---- observations / candidats
        out["snapshots"]["datapoints"] = dbm.query(
            "SELECT COUNT(*) AS c FROM web_datapoints", one=True)["c"]
        out["snapshots"]["candidates"] = dbm.query(
            "SELECT COUNT(*) AS c FROM data_snapshots WHERE source=%s",
            ("web4-candidate",), one=True)["c"]
        out["snapshots"]["entities"] = dbm.query(
            "SELECT COUNT(*) AS c FROM web_entities", one=True)["c"]
        out["snapshots"]["venues"] = dbm.query(
            "SELECT COUNT(*) AS c FROM web_venues", one=True)["c"]
        # ---- sources (id + host uniquement — §30)
        reg = registry
        if reg is None and _RUNTIME["client"] is not None:
            reg = getattr(_RUNTIME["client"], "reg", None)
        if reg:
            from sources import registry as _regmod
            for sid in ("espn", "football_data_co_uk", "open_meteo",
                        "openligadb", "statsbomb_open", "wikidata"):
                src = _regmod.get_source(reg, sid) or {}
                entry = {"status": src.get("status"),
                         "enabled": bool(src.get("enabled")),
                         "host": _host_of(src.get("base_url"))}
                if _RUNTIME["client"] is not None:
                    try:
                        entry["circuit"] = _RUNTIME["client"].circuit_state(sid)["state"]
                    except Exception:
                        pass
                errs = dbm.query(
                    """SELECT COUNT(*) AS c FROM web_research_events
                       WHERE source=%s AND status='ERROR'""", (sid,),
                    one=True)["c"]
                entry["errors_total"] = errs
                out["sources"][sid] = entry
        # ---- scheduler
        sched = _RUNTIME["scheduler"]
        if sched is not None:
            out["scheduler"].update({
                "tick_sec": sched.config["tick_sec"],
                "matches_per_cycle": sched.config["matches_per_cycle"],
                "next_tiers": [t for t in ("calendar", "T-180", "T-60", "T-15")
                               if sched.due(t)],
            })
        out["alerts"] = [dict(a) for a in _metrics.recent_alerts(
            limit=10, db_module=dbm)]
        # 2C.1 — diagnostic shadow (compteurs INTERNES §16 — jamais de
        # probabilités, jamais exposé au frontend ; additif et défensif)
        try:
            from model2c import shadow_monitor as _smon
            out["shadow2c"] = _smon.status_block(dbm)
        except Exception as e2:
            out["shadow2c"] = {"enabled": None, "status": "unavailable",
                               "error": type(e2).__name__}
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def healthz_block(registry=None, db_module=None):
    """Bloc ADDITIF pour /healthz (§31) — léger, jamais d'exception ;
    le cœur (ok/integrity/build/migration) reste inchangé."""
    try:
        ok = last_successful_cycle(db_module)
        return {"enabled": _RUNTIME["scheduler"] is not None,
                "core_health": "ok",
                "data_health": data_health(db_module),
                "last_successful_cycle": (ok or {}).get("finished_at")}
    except Exception:
        return {"enabled": False, "core_health": "ok",
                "data_health": "unknown", "last_successful_cycle": None}
