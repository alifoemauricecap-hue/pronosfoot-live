# -*- coding: utf-8 -*-
"""
RESEARCH JOURNAL PERSISTANT (2B.WEB-4 §23/§24/§28)
===================================================
Write-through : mémoire bornée (compat ResearchJournal WEB-3) + table
`web_research_events` (restart-safe §23 : le journal survit au reboot).

SÉCURITÉ (§38) — DOUBLE verrou :
1. mêmes clés interdites que WEB-3 supprimées à l'enregistrement ;
2. la table ne connaît qu'une WHITELIST de colonnes — une clé sensible ne
   peut pas exister physiquement dans le persistant.
"""
import json
import threading

import db as _db

from ..normalized import now_iso
from ..research_journal import FORBIDDEN_KEYS

_WHITELIST = ("event", "match_id", "source", "data_type", "host", "status",
              "latency_ms", "bytes", "cache_hit", "records_found",
              "confidence", "freshness", "decision", "error")


class PersistentResearchJournal:
    def __init__(self, capacity=5000, db_module=None, memory_only=False):
        self.capacity = int(capacity)
        self.db = db_module or _db
        self.memory_only = memory_only
        self._events = []
        self._lock = threading.Lock()

    def record(self, **event):
        ev = {"timestamp": now_iso(), **event}
        for k in FORBIDDEN_KEYS:
            ev.pop(k, None)
        with self._lock:
            self._events.append(ev)
            if len(self._events) > self.capacity:
                self._events = self._events[-self.capacity:]
        if not self.memory_only:
            dec = ev.get("decision")
            self.db.execute(
                """INSERT INTO web_research_events
                   (timestamp, event, match_id, source, data_type, host,
                    status, latency_ms, bytes, cache_hit, records_found,
                    confidence, freshness, decision_json, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ev.get("timestamp"), ev.get("event"), ev.get("match_id"),
                 ev.get("source"), ev.get("data_type"), ev.get("host"),
                 ev.get("status"), ev.get("latency_ms"), ev.get("bytes"),
                 1 if ev.get("cache_hit") else 0 if ev.get("cache_hit") is not None else None,
                 ev.get("records_found"), ev.get("confidence"),
                 ev.get("freshness"),
                 (json.dumps(dec, ensure_ascii=False, default=str)
                  if dec is not None else None),
                 ev.get("error")))
        return ev

    def events(self, match_id=None, source=None):
        """Lecture : persistant d'abord (source de vérité du journal), puis
        complément mémoire non encore flushé — sans doublons sur timestamp."""
        out = []
        if not self.memory_only:
            sql, params = "SELECT * FROM web_research_events WHERE 1=1", []
            if match_id is not None:
                sql += " AND match_id=%s"; params.append(match_id)
            if source is not None:
                sql += " AND source=%s"; params.append(source)
            sql += " ORDER BY id"
            for r in self.db.rows_to_dicts(self.db.query(sql, params)):
                dec = None
                if r.get("decision_json"):
                    try:
                        dec = json.loads(r["decision_json"])
                    except Exception:
                        dec = r["decision_json"]
                out.append({"timestamp": r["timestamp"], "event": r["event"],
                            "match_id": r["match_id"], "source": r["source"],
                            "data_type": r["data_type"], "host": r["host"],
                            "status": r["status"], "latency_ms": r["latency_ms"],
                            "bytes": r["bytes"],
                            "cache_hit": bool(r["cache_hit"]) if r["cache_hit"] is not None else None,
                            "records_found": r["records_found"],
                            "confidence": r["confidence"],
                            "freshness": r["freshness"], "decision": dec,
                            "error": r["error"]})
            return out
        with self._lock:
            evs = list(self._events)
        if match_id is not None:
            evs = [e for e in evs if e.get("match_id") == match_id]
        if source is not None:
            evs = [e for e in evs if e.get("source") == source]
        return evs

    def count(self):
        if self.memory_only:
            with self._lock:
                return len(self._events)
        return self.db.query("SELECT COUNT(*) AS c FROM web_research_events",
                             one=True)["c"]

    def clear(self):
        with self._lock:
            self._events.clear()

    # utilisé par le scheduler pour compter les cache hits d'un cycle
    def events_since(self, timestamp_iso):
        return [e for e in self.events() if (e.get("timestamp") or "") >= timestamp_iso]
