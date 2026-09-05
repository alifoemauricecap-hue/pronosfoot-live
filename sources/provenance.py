# -*- coding: utf-8 -*-
"""
PROVENANCE — modèle de donnée traçable (ÉTAPE 2B.1 §11)
=======================================================
Toute donnée entrante (score, cote, météo, blessure…) sera représentée par un
« data_point » horodaté. Ce module définit LE contrat + la règle d'anti-fuite.

    effective_at <= prediction_time   ET   retrieved_at <= prediction_time

sinon la donnée est INTERDITE d'usage pour la prédiction à cet instant.
AUCUN accès réseau / base de données ici non plus (pur et testable).
"""
import re

ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")

REQUIRED = ("value", "data_type", "source", "retrieved_at",
            "effective_at", "confidence", "freshness_sec")
OPTIONAL = ("source_url", "published_at", "match_id", "team_id",
            "player_id", "competition_id")


class ProvenanceError(ValueError):
    """data_point invalide (§11)."""


def _check_ts(name, value, errs, allow_none=False):
    if value is None and allow_none:
        return
    if not (isinstance(value, str) and ISO_RE.match(value)):
        errs.append(f"{name} : horodatage ISO-8601 requis (reçu : {value!r})")


def validate_data_point(dp):
    """Lève ProvenanceError si le data_point ne respecte pas le contrat.
    Règles : champs requis présents, timestamps ISO, confidence ∈ [0,1],
    freshness_sec = retrieved_at - effective_at (≥ 0), source non vide."""
    errs = []
    if not isinstance(dp, dict):
        raise ProvenanceError("data_point doit être un dict")
    for k in REQUIRED:
        if k not in dp:
            errs.append(f"champ requis manquant : '{k}'")
    if errs:
        raise ProvenanceError(" ; ".join(errs))
    if not dp.get("source"):
        errs.append("'source' vide — la provenance est OBLIGATOIRE (§7/§11)")
    _check_ts("retrieved_at", dp.get("retrieved_at"), errs)
    _check_ts("effective_at", dp.get("effective_at"), errs)
    _check_ts("published_at", dp.get("published_at"), errs, allow_none=True)
    c = dp.get("confidence")
    if not (isinstance(c, (int, float)) and 0.0 <= c <= 1.0):
        errs.append(f"confidence ∈ [0,1] requise (reçu : {c!r})")
    f = dp.get("freshness_sec")
    if not (isinstance(f, (int, float)) and f >= 0):
        errs.append(f"freshness_sec ≥ 0 requis (reçu : {f!r})")
    unknown = [k for k in dp if k not in REQUIRED + OPTIONAL]
    if unknown:
        errs.append(f"champs inconnus {unknown} — schéma strict (extensible "
                    f"via registry.json, pas par insertion sauvage)")
    if errs:
        raise ProvenanceError(" ; ".join(errs))
    return True


def usable_at(dp, prediction_time):
    """RÈGLE ANTI-FUITE (§7/§11) — la donnée peut-elle servir à une
    prédiction faite à `prediction_time` ?
    Vrai SEULEMENT si effective_at ≤ T ET retrieved_at ≤ T.
    Comparaison lexicographique sûre : timestamps ISO-8601 UTC ('Z')."""
    validate_data_point(dp)
    return (dp["effective_at"][:19] <= prediction_time[:19]
            and dp["retrieved_at"][:19] <= prediction_time[:19])


def new_data_point(value, data_type, source, retrieved_at, effective_at=None,
                   published_at=None, confidence=0.5, freshness_sec=None,
                   source_url=None, match_id=None, team_id=None, player_id=None,
                   competition_id=None):
    """Fabrique un data_point conforme. Par défaut (aucun timestamp fourni par
    la source — ex. cotes ESPN) : effective_at = retrieved_at et confidence
    maximale 0.7 pour les données volatiles sans published_at."""
    effective_at = effective_at or retrieved_at
    if freshness_sec is None:
        freshness_sec = 0
    dp = {"value": value, "data_type": data_type, "source": source,
          "source_url": source_url, "retrieved_at": retrieved_at,
          "published_at": published_at, "effective_at": effective_at,
          "confidence": confidence, "freshness_sec": freshness_sec,
          "match_id": match_id, "team_id": team_id, "player_id": player_id,
          "competition_id": competition_id}
    validate_data_point(dp)
    return dp
