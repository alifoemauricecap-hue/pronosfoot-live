# -*- coding: utf-8 -*-
"""
POINT-IN-TIME DATA STORE (2B.WEB-3 §3/§14/§24)
===============================================
Magasin de DataPoints indexé par (match, type, temps). Règles :
- JAMAIS de mutation d'un DataPoint existant (append-only) : une nouvelle
  information à T-5 crée une NOUVELLE entrée — le passé n'est pas réécrit ;
- la SELECTIVITÉ temporelle est appliquée À LA LECTURE : query(as_of=T)
  ne retourne que les points usable_at(T) (anti-leakage §14) ;
- ce store n'est PAS la DB 2A et ne la remplace pas : c'est le tapis
  roulant qui alimente le SNAPSHOT BUILDER 2A (snapshots.py), qui est la
  seule voie d'écriture persistante (insert-only, hashé).
"""
import threading

from .normalized import DataPoint, is_unknown


class PointInTimeStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._points = []                      # append-only

    def add(self, dp):
        if not isinstance(dp, DataPoint):
            raise TypeError("PointInTimeStore n'accepte que des DataPoint")
        with self._lock:
            self._points.append(dp)
        return dp

    def add_many(self, dps):
        for dp in dps:
            self.add(dp)
        return dps

    def query(self, match_id=None, data_type=None, team_id=None,
              source=None, as_of=None, include_unknown=True,
              only_valid=True):
        """Lecture filtrée. as_of=T ⇒ SEULEMENT les points utilisables à T
        (retrieved_at<=T ET effective_at<=T). Sans as_of : tout l'historique."""
        with self._lock:
            pts = list(self._points)
        out = []
        for dp in pts:
            if match_id is not None and dp.match_id != match_id:
                continue
            if data_type is not None and dp.data_type != data_type:
                continue
            if team_id is not None and dp.team_id != team_id:
                continue
            if source is not None and dp.source != source:
                continue
            if only_valid and not dp.valid:
                continue
            if not include_unknown and is_unknown(dp.value):
                continue
            if as_of is not None and not dp.usable_at(as_of):
                continue
            out.append(dp)
        return out

    def latest(self, match_id, data_type, as_of, **kw):
        """Le point le plus récent (par retrieved_at) utilisable à as_of."""
        pts = self.query(match_id=match_id, data_type=data_type, as_of=as_of,
                         **kw)
        if not pts:
            return None
        return max(pts, key=lambda p: (p.retrieved_at or ""))

    def count(self):
        with self._lock:
            return len(self._points)

    def dump(self):
        with self._lock:
            return list(self._points)
