# -*- coding: utf-8 -*-
"""
POINT-IN-TIME STORE PERSISTANT (2B.WEB-4 §5/§6)
================================================
Version SQLite de PointInTimeStore (WEB-3), MÊME INTERFACE :
add / add_many / query / latest / count / dump.

Règles ABSOLUES :
- APPEND-ONLY : chaque nouvelle observation = une NOUVELLE ligne. JAMAIS
  d'UPDATE d'une observation historique (versionnage §6 : ESPN 14:00 → 8,
  ESPN 14:05 → 10 — les DEUX lignes sont conservées) ;
- `dedupe_key` bloque UNIQUEMENT le doublon EXACT (même type, même valeur,
  même source, même retrieved_at) — jamais une version distincte ;
- la sélectivité temporelle reste À LA LECTURE : query(as_of=T) ne retourne
  que les points usable_at(T) — anti-leakage §14 (CONFUSION ABSOLUE entre
  « persister » et « rendre utilisable ») ;
- « Que savait PronoFoot à 14:02 ? » = query(as_of="…14:02…").
"""
import hashlib
import json
import threading

import db as _db

from ..normalized import DataPoint, UNKNOWN, is_unknown


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def dedupe_key(dp):
    """Identité EXACTE de l'observation : même source, même valeur, même
    instant de capture ⇒ UN SEUL exemplaire. Deux instants différents ⇒
    deux lignes (versionnage §6)."""
    return _sha(_canonical({
        "dt": dp.data_type, "src": dp.source,
        "v": ("UNKNOWN" if is_unknown(dp.value) else dp.value),
        "r": dp.retrieved_at, "e": dp.effective_at,
        "mid": dp.match_id, "tid": dp.team_id, "pid": dp.player_id,
        "cid": dp.competition_id, "lvl": dp.level}))


def checksum_of(dp):
    return _sha(_canonical({
        "v": ("UNKNOWN" if is_unknown(dp.value) else dp.value),
        "dt": dp.data_type, "lvl": dp.level, "src": dp.source,
        "r": dp.retrieved_at, "valid": dp.valid,
        "conf": dp.confidence}))


class PersistentPITStore:
    """Store point-in-time append-only, persistant (restart-safe §23)."""

    def __init__(self, db_module=None):
        self.db = db_module or _db
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ écriture
    def add(self, dp):
        if not isinstance(dp, DataPoint):
            raise TypeError("PersistentPITStore n'accepte que des DataPoint")
        dk = dedupe_key(dp)
        with self._lock:
            try:
                self.db.execute(
                    """INSERT INTO web_datapoints
                       (id, dedupe_key, match_id, team_id, player_id,
                        competition_id, data_type, level, value_json,
                        is_unknown, source_id, source_url, retrieved_at,
                        published_at, effective_at, confidence, valid,
                        issues_json, checksum, derivation_method,
                        model_version, inputs_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (dk[:32], dk, dp.match_id, dp.team_id, dp.player_id,
                     dp.competition_id, dp.data_type, dp.level,
                     None if is_unknown(dp.value) else _canonical(dp.value),
                     1 if is_unknown(dp.value) else 0, dp.source,
                     dp.source_url, dp.retrieved_at, dp.published_at,
                     dp.effective_at, dp.confidence, 1 if dp.valid else 0,
                     _canonical(list(dp.issues)), checksum_of(dp),
                     dp.derivation_method, dp.model_version,
                     _canonical(list(dp.inputs)), self.db.utcnow()))
            except Exception as e:
                if "UNIQUE" not in str(e).upper():
                    raise
                # doublon EXACT → ignoré (idempotent, jamais écrasé)
        return dp

    def add_many(self, dps):
        for dp in dps:
            self.add(dp)
        return dps

    # ------------------------------------------------------------------ lecture
    @staticmethod
    def _row_to_point(r):
        is_unk = bool(r["is_unknown"])
        value = UNKNOWN if is_unk else json.loads(r["value_json"])
        dp = DataPoint(
            value, r["data_type"], r["source_id"], r["retrieved_at"],
            level=r["level"], source_url=r["source_url"],
            published_at=r["published_at"], effective_at=r["effective_at"],
            confidence=r["confidence"] or "unknown",
            match_id=r["match_id"], team_id=r["team_id"],
            player_id=r["player_id"], competition_id=r["competition_id"],
            valid=bool(r["valid"]),
            issues=json.loads(r["issues_json"] or "[]"),
            derivation_method=r["derivation_method"],
            model_version=r["model_version"],
            inputs=json.loads(r["inputs_json"] or "[]"))
        return dp

    def query(self, match_id=None, data_type=None, team_id=None,
              source=None, as_of=None, include_unknown=True,
              only_valid=True):
        sql, params = ("SELECT * FROM web_datapoints WHERE 1=1"), []
        if match_id is not None:
            sql += " AND match_id=%s"; params.append(match_id)
        if data_type is not None:
            sql += " AND data_type=%s"; params.append(data_type)
        if team_id is not None:
            sql += " AND team_id=%s"; params.append(team_id)
        if source is not None:
            sql += " AND source_id=%s"; params.append(source)
        if only_valid:
            sql += " AND valid=1"
        if not include_unknown:
            sql += " AND is_unknown=0"
        sql += " ORDER BY retrieved_at, id"
        rows = self.db.rows_to_dicts(self.db.query(sql, params))
        out = [self._row_to_point(r) for r in rows]
        if as_of is not None:
            out = [dp for dp in out if dp.usable_at(as_of)]   # §14 anti-leakage
        return out

    def latest(self, match_id, data_type, as_of, **kw):
        pts = self.query(match_id=match_id, data_type=data_type, as_of=as_of,
                         **kw)
        if not pts:
            return None
        return max(pts, key=lambda p: (p.retrieved_at or ""))

    def count(self):
        return self.db.query("SELECT COUNT(*) AS c FROM web_datapoints",
                             one=True)["c"]

    def dump(self):
        return self.query(only_valid=False)

    # « Que savait PronoFoot à T ? » (§6) — projection anti-leakage explicite
    def known_at(self, match_id, as_of):
        return self.query(match_id=match_id, as_of=as_of, only_valid=False)
