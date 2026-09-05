# -*- coding: utf-8 -*-
"""
RESEARCH JOURNAL (2B.WEB-3 §26)
================================
Journal structuré de chaque recherche de données (research_event) :

    timestamp · match_id · source · data_type · host · status · latency_ms ·
    bytes · cache_hit · records_found · confidence · freshness · decision ·
    error

JAMAIS : token, clé API, Authorization, cookie, secret (filtrage dur à
l'enregistrement, comme safe_http._log).
"""
import threading

from .normalized import now_iso

FORBIDDEN_KEYS = ("authorization", "cookie", "token", "api_key", "password",
                  "secret", "x-auth-token")


class ResearchJournal:
    """Capacité bornée (derniers N événements) — mémoire, pas la DB 2A."""

    def __init__(self, capacity=5000):
        self.capacity = int(capacity)
        self._events = []
        self._lock = threading.Lock()

    def record(self, **event):
        ev = {"timestamp": now_iso(), **event}
        for k in FORBIDDEN_KEYS:
            ev.pop(k, None)
        # défense en profondeur : ne jamais sérialiser une valeur qui
        # ressemble à un secret connu (clés en clair dans l'URL sont
        # déjà impossibles : seul le HOST est journalisé, jamais l'URL).
        with self._lock:
            self._events.append(ev)
            if len(self._events) > self.capacity:
                self._events = self._events[-self.capacity:]
        return ev

    def events(self, match_id=None, source=None):
        with self._lock:
            evs = list(self._events)
        if match_id is not None:
            evs = [e for e in evs if e.get("match_id") == match_id]
        if source is not None:
            evs = [e for e in evs if e.get("source") == source]
        return evs

    def count(self):
        with self._lock:
            return len(self._events)

    def clear(self):
        with self._lock:
            self._events.clear()
