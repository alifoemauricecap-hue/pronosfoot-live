# -*- coding: utf-8 -*-
"""
VALIDATION DES DONNÉES (2B.WEB-3 §17)
======================================
Validateurs de cohérence métier. Règles :
- une anomalie ne PLANTE jamais l'application ;
- elle est journalisée (issue code sur le DataPoint, valid=False) ;
- une donnée invalide N'EST JAMAIS injectée silencieusement dans le modèle —
  elle devient INVALID (conservée pour audit) et la couche décision la
  traite comme indisponible ;
- UNKNOWN n'est PAS une anomalie (donnée absente ≠ donnée aberrante).
"""
from .normalized import DataPoint, UNKNOWN, is_unknown

RULES = (
    "score >= 0", "possession ∈ [0,100]", "probabilité ∈ [0,1]",
    "cote > 1", "minute ∈ [0,130]", "compteurs ≥ 0",
    "shots_on_target <= shots", "équipes distinctes", "date ISO valide",
)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check_non_negative(dp):
    if _num(dp.value) and dp.value < 0:
        return "INVALID_NEGATIVE"
    return None


def check_score(dp):
    if _num(dp.value):
        if dp.value < 0:
            return "INVALID_SCORE_NEGATIVE"
        if dp.value > 99:
            return "INVALID_SCORE_ABSURDE"
    return None


def check_possession(dp):
    if _num(dp.value) and not (0 <= dp.value <= 100):
        return "INVALID_POSSESSION_RANGE"
    return None


def check_probability(dp):
    if _num(dp.value) and not (0 <= dp.value <= 1):
        return "INVALID_PROBABILITY_RANGE"
    return None


def check_odd(dp):
    if _num(dp.value) and dp.value <= 1:
        return "INVALID_ODD_LE_1"
    return None


def check_minute(dp):
    if _num(dp.value) and not (0 <= dp.value <= 130):
        return "INVALID_MINUTE_RANGE"
    return None


# table data_type → validateurs (appliqués uniquement si valeur numérique)
_BY_TYPE = {
    "score_home": (check_score,), "score_away": (check_score,),
    "possession": (check_possession,),
    "shots": (check_non_negative,), "shots_on_target": (check_non_negative,),
    "corners": (check_non_negative,), "fouls": (check_non_negative,),
    "cards_yellow": (check_non_negative,), "cards_red": (check_non_negative,),
    "goals_for": (check_non_negative,), "goals_against": (check_non_negative,),
    "minute": (check_minute,),
    "odd": (check_odd,), "xg": (check_non_negative,), "xga": (check_non_negative,),
    "probability": (check_probability,),
    "attacks": (check_non_negative,), "dangerous_attacks": (check_non_negative,),
}


def validate_datapoint(dp, context=None):
    """Retourne la liste des issues (vide = valide). Ne lève jamais.
    UNKNOWN → aucune validation numérique (absence ≠ aberration)."""
    issues = []
    if not isinstance(dp, DataPoint):
        return ["INVALID_STRUCTURE"]
    if is_unknown(dp.value):
        return issues                          # UNKNOWN n'est pas invalidé
    for fn in _BY_TYPE.get(dp.data_type, ()):
        r = fn(dp)
        if r:
            issues.append(r)
    # cohérences croisées via context facultatif
    if context:
        sot, shots = context.get("shots_on_target"), context.get("shots")
        if _num(sot) and _num(shots) and sot > shots:
            issues.append("INVALID_SOT_GT_SHOTS")
        home, away = context.get("home_team"), context.get("away_team")
        if home is not None and home == away:
            issues.append("INVALID_SAME_TEAM")
    return issues


def apply_validation(dp, context=None):
    """Marque le DataPoint (valid=False + issues) si anomalie. Jamais d'except."""
    issues = validate_datapoint(dp, context=context)
    for i in issues:
        if i not in dp.issues:
            dp.issues.append(i)
    if issues:
        dp.valid = False
    return dp


def is_usable_value(dp):
    """Valeur injectable ailleurs ? (valide + présente)."""
    return isinstance(dp, DataPoint) and dp.valid and not is_unknown(dp.value)
