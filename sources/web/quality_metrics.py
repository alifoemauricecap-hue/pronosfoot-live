# -*- coding: utf-8 -*-
"""
QUALITY METRICS (2B.WEB-3 §34)
==============================
Métriques de QUALITÉ DES DONNÉES — compteurs techniques purs.

CE NE SONT PAS des métriques de modèle : accuracy, Brier Score, Log Loss
et calibration appartiennent au système d'évaluation des PRÉDICTIONS et
n'ont RIEN à faire ici. Ce module n'en calcule aucune et n'en dérive
aucune (garde-fou explicite).
"""
import threading

_MODEL_METRIC_WHITELIST_FORBIDDEN = ("accuracy", "brier", "log_loss",
                                     "calibration")


class QualityMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.fetch_ok = 0
        self.fetch_fail = 0
        self.latency_total_ms = 0.0
        self.cache_hits = 0
        self.requests_total = 0
        self.requests_per_match = {}           # match_id -> count
        self.datapoints_total = 0
        self.datapoints_unknown = 0
        self.datapoints_invalid = 0
        self.conflicts_total = 0
        self.conflicts_resolved = 0
        self.conflicts_unknown = 0
        self.fallbacks_total = 0
        self.identity_matched = 0
        self.identity_unmatched = 0
        self.identity_unknown = 0
        self.stale_rejected = 0

    def record_fetch(self, match_id=None, ok=True, latency_ms=0.0,
                     cache_hit=False):
        with self._lock:
            if cache_hit:
                self.cache_hits += 1
            else:
                self.requests_total += 1
                if match_id:
                    self.requests_per_match[match_id] = \
                        self.requests_per_match.get(match_id, 0) + 1
            if ok:
                self.fetch_ok += 1
            else:
                self.fetch_fail += 1
            self.latency_total_ms += float(latency_ms or 0.0)

    def record_datapoint(self, *, unknown=False, valid=True):
        with self._lock:
            self.datapoints_total += 1
            if unknown:
                self.datapoints_unknown += 1
            if not valid:
                self.datapoints_invalid += 1

    def record_conflict(self, resolved=None):
        with self._lock:
            self.conflicts_total += 1
            if resolved is True:
                self.conflicts_resolved += 1
            elif resolved is None:
                self.conflicts_unknown += 1

    def record_fallback(self):
        with self._lock:
            self.fallbacks_total += 1

    def record_identity(self, status):
        with self._lock:
            if status in ("MATCH_AUTO", "MATCH_JOURNALIZED"):
                self.identity_matched += 1
            elif status == "NO_MATCH":
                self.identity_unmatched += 1
            else:
                self.identity_unknown += 1

    def record_stale_rejected(self):
        with self._lock:
            self.stale_rejected += 1

    def snapshot(self):
        with self._lock:
            f = self.fetch_ok + self.fetch_fail
            d = self.datapoints_total or 1
            ids = self.identity_matched + self.identity_unmatched \
                + self.identity_unknown
            return {
                "source_success_rate": (self.fetch_ok / f) if f else None,
                "source_failure_rate": (self.fetch_fail / f) if f else None,
                "source_latency_avg_ms":
                    round(self.latency_total_ms / f, 2) if f else None,
                "cache_hit_rate":
                    round(self.cache_hits /
                          (self.requests_total + self.cache_hits), 4)
                    if (self.requests_total + self.cache_hits) else None,
                "unknown_rate": round(self.datapoints_unknown / d, 4),
                "invalid_data_rate": round(self.datapoints_invalid / d, 4),
                "conflict_rate": round(self.conflicts_total / d, 4),
                "conflict_resolution_rate":
                    (round(self.conflicts_resolved / self.conflicts_total, 4)
                     if self.conflicts_total else None),
                "fallback_rate":
                    round(self.fallbacks_total / self.requests_total, 4)
                    if self.requests_total else None,
                "identity_match_rate":
                    (round(self.identity_matched / ids, 4) if ids else None),
                "requests_total": self.requests_total,
                "requests_per_match": dict(self.requests_per_match),
                "stale_rejected": self.stale_rejected,
                "datapoints_total": self.datapoints_total,
            }

    @staticmethod
    def assert_no_model_metrics(snap):
        """Garde-fou §34 : aucune métrique de modèle ne doit apparaître."""
        for bad in _MODEL_METRIC_WHITELIST_FORBIDDEN:
            assert bad not in snap, f"métrique de modèle interdite : {bad}"
