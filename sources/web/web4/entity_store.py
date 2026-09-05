# -*- coding: utf-8 -*-
"""
ENTITY MAP PERSISTANTE (2B.WEB-4 §17/§18)
==========================================
Correspondances entités internes ↔ identifiants externes par source
(ESPN id, football-data nom CSV, OpenLigaDB id, StatsBomb id, Wikidata QID).

RÈGLES (§17) :
- JAMAIS de mapping automatique incertain : les seuils WEB-1 sont appliqués
    MATCH_AUTO      ≥ 0.95  → mapping actif
    JOURNALIZED 0.80–0.95   → mapping stocké MAIS marqué (révisable)
    UNKNOWN         < 0.80  → RIEN de stocké comme mapping (evidence seule)
    contradiction majeure    → NO_MATCH : refus + alerte WARNING, jamais
                               d'écrasement d'un mapping existant ;
- catégories distinctes (§18) : équipe première ≠ réserve ≠ U19 ≠ féminine —
  la clé interne normalisée porte la catégorie détectée par WEB-1 ;
- entity_map.json reste la GRaine declarative ; ce store est le mécanisme
  persistant équivalent prévu par §17 (« ou le mécanisme persistant
  équivalent »).
"""
import json

import db as _db

from .. import identity as _id
from .. import decision as _dec
from ..normalized import now_iso
from .metrics import raise_alert, WARNING

TEAM = "team"
COMPETITION = "competition"
VENUE = "venue"
PLAYER = "player"
ENTITY_TYPES = (TEAM, COMPETITION, VENUE, PLAYER)

#: statuts autorisant une RÉSOLUTION (AUTO = confiance pleine,
#: JOURNALIZED = utilisable mais marqué révisable)
RESOLVABLE = (_dec.MATCH_AUTO, _dec.MATCH_JOURNALIZED)


def team_key(name):
    """Clé interne stable d'une équipe : nom normalisé WEB-1 + catégorie
    (hommes/femmes/jeunes/réserve) — PSG féminin ≠ PSG masculin (§18)."""
    norm = _id.normalize_team_name(name, strip_suffixes=True)
    cat = _id.team_category(norm) or "senior"
    return f"{norm}|{cat}"


class EntityStore:
    def __init__(self, db_module=None):
        self.db = db_module or _db

    # ------------------------------------------------------------------ write
    def propose(self, entity_type, internal_key, source_id, external_id,
                external_name=None, confidence=0.0, evidence=None,
                decided_by="web4"):
        """Propose un mapping. Retourne un dict de décision EXPLICITE.
        Ne crée JAMAIS un mapping incertain (§17)."""
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"entity_type inconnu : {entity_type}")
        if not internal_key or not source_id or not external_id:
            return {"stored": False, "status": _dec.UNKNOWN,
                    "reason": "MISSING_FIELDS"}
        status, reason = _dec.decide_status(float(confidence or 0.0), ())
        if status in (_dec.NO_MATCH, _dec.UNKNOWN):
            return {"stored": False, "status": status, "reason": reason,
                    "confidence": confidence}
        # contradiction : le même internal_key déjà lié à un AUTRE id externe
        clash = self.db.query(
            """SELECT external_id, status FROM web_entities
               WHERE entity_type=%s AND source_id=%s AND internal_key=%s
               AND external_id<>%s LIMIT 1""",
            (entity_type, source_id, internal_key, str(external_id)),
            one=True)
        if clash:
            raise_alert(WARNING, "ENTITY_CONTRADICTION",
                        {"entity_type": entity_type, "source_id": source_id,
                         "internal_key": internal_key,
                         "existing_external_id": clash["external_id"],
                         "rejected_external_id": str(external_id)},
                        db_module=self.db)
            return {"stored": False, "status": "CONTRADICTION",
                    "existing_external_id": clash["external_id"]}
        now = now_iso()
        self.db.execute(
            """INSERT INTO web_entities
               (entity_type, internal_key, source_id, external_id,
                external_name, confidence, status, decided_by, decided_at,
                evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(entity_type, source_id, external_id) DO UPDATE SET
                 confidence=MAX(confidence, excluded.confidence),
                 status=CASE WHEN status='MATCH_AUTO' THEN status
                             ELSE excluded.status END,
                 external_name=COALESCE(excluded.external_name, external_name),
                 decided_at=excluded.decided_at,
                 evidence_json=excluded.evidence_json""",
            (entity_type, internal_key, source_id, str(external_id),
             external_name, float(confidence), status, decided_by, now,
             json.dumps(evidence, ensure_ascii=False, default=str)
             if evidence is not None else None))
        return {"stored": True, "status": status, "reason": reason,
                "confidence": confidence}

    def propose_team(self, team_name, source_id, external_id,
                     external_name=None, confidence=0.0, evidence=None):
        return self.propose(TEAM, team_key(team_name), source_id, external_id,
                            external_name=external_name or team_name,
                            confidence=confidence, evidence=evidence)

    # ------------------------------------------------------------------ read
    def resolve(self, entity_type, source_id, internal_key):
        """internal_key → external_id (mappings AUTO/JOURNALIZED seulement)."""
        rows = self.db.query(
            """SELECT external_id, status, confidence FROM web_entities
               WHERE entity_type=%s AND source_id=%s AND internal_key=%s
               AND status IN ('MATCH_AUTO','MATCH_JOURNALIZED')
               ORDER BY confidence DESC LIMIT 1""",
            (entity_type, source_id, internal_key), one=True)
        return rows["external_id"] if rows else None

    def reverse(self, entity_type, source_id, external_id):
        """external_id → internal_key (mappings AUTO/JOURNALIZED seulement)."""
        r = self.db.query(
            """SELECT internal_key FROM web_entities
               WHERE entity_type=%s AND source_id=%s AND external_id=%s
               AND status IN ('MATCH_AUTO','MATCH_JOURNALIZED') LIMIT 1""",
            (entity_type, source_id, str(external_id)), one=True)
        return r["internal_key"] if r else None

    def resolve_team(self, team_name, source_id):
        return self.resolve(TEAM, source_id, team_key(team_name))

    def get(self, entity_type, source_id, external_id):
        r = self.db.query(
            """SELECT * FROM web_entities
               WHERE entity_type=%s AND source_id=%s AND external_id=%s""",
            (entity_type, source_id, str(external_id)), one=True)
        return dict(r) if r else None

    def count(self, status=None):
        if status:
            return self.db.query(
                "SELECT COUNT(*) AS c FROM web_entities WHERE status=%s",
                (status,), one=True)["c"]
        return self.db.query("SELECT COUNT(*) AS c FROM web_entities",
                             one=True)["c"]

    def export_map(self):
        """Projection au format entity_map.json (§17 — révisable, exportable)."""
        out = {"version": "2B.WEB-4",
               "match_ids": {}, "team_ids": {}, "player_ids": {},
               "competition_ids": {}}
        bucket = {TEAM: "team_ids", PLAYER: "player_ids",
                  COMPETITION: "competition_ids"}.get
        for r in self.db.rows_to_dicts(self.db.query(
                "SELECT * FROM web_entities ORDER BY id")):
            b = bucket(r["entity_type"])
            if b is None:
                continue
            out[b].setdefault(r["internal_key"], {})[r["source_id"]] = {
                "external_id": r["external_id"],
                "status": r["status"], "confidence": r["confidence"],
                "decided_at": r["decided_at"]}
        return out
