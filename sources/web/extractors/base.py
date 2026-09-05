# -*- coding: utf-8 -*-
"""
EXTRACTORS — base commune (2B.WEB-3 §4)
========================================
Chaque extracteur est PUR : il reçoit un payload (dict/list/str/bytes) et
produit des DataPoint normalisés. Il ne fait JAMAIS de réseau — c'est le
rôle du SafeHttpClient (WEB-2), le SEUL chemin réseau autorisé (§27).

CONTRAT ANTI-INVENTION (§R1) :
- un extracteur n'émet QUE ce qui est réellement présent dans la réponse ;
- champ absent → AUCUN DataPoint pour ce champ (la couverture UNKNOWN est
  produite en amont par le pipeline, explicitement) ;
- valeur invalide → issue + valid=False, jamais de correction silencieuse.
"""
import json

from ..normalized import DataPoint, new_datapoint, SOURCE_NATIVE, NORMALIZED


class FetchContext:
    """Contexte d'extraction : cibles + timestamps mesurés par la couche HTTP."""
    __slots__ = ("match_id", "team_id", "player_id", "competition_id",
                 "retrieved_at", "source_url", "confidence", "extras")

    def __init__(self, match_id=None, team_id=None, player_id=None,
                 competition_id=None, retrieved_at=None, source_url=None,
                 confidence="high", **extras):
        self.match_id = match_id
        self.team_id = team_id
        self.player_id = player_id
        self.competition_id = competition_id
        self.retrieved_at = retrieved_at
        self.source_url = source_url
        self.confidence = confidence
        self.extras = dict(extras)


def ensure_obj(body):
    """payload brut (bytes/str) → objet JSON. Lève ValueError (malformé)."""
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        text = body.strip()
        if not text:
            raise ValueError("REPONSE_VIDE")
        return json.loads(text)
    return body


def point(value, data_type, source, ctx, *, level=NORMALIZED, effective_at=None,
          confidence=None, issues=None, team_id=None, match_id=None,
          published_at=None, **kw):
    """DataPoint standardisé pour un extracteur (provenance complète)."""
    return new_datapoint(
        value, data_type, source, ctx.retrieved_at,
        level=level, source_url=ctx.source_url, published_at=published_at,
        effective_at=effective_at,
        confidence=confidence or ctx.confidence,
        match_id=match_id if match_id is not None else ctx.match_id,
        team_id=team_id if team_id is not None else ctx.team_id,
        competition_id=ctx.competition_id, issues=issues, **kw)
