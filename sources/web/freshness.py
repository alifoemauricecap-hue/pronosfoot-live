"""
FRESHNESS ENGINE (2B.WEB-3 §15)
================================
TTL pilotés par le REGISTRY 2B.1 (ttl_defaults_sec + ttl_overrides_sec) —
LA source de vérité, jamais de table parallèle ici.

    score_live 30 s · match_calendar 900 · odds 300 · lineups 300 ·
    injuries 1800 · standings 7200 · weather 10800 · historical/xG : sans
    expiration (None)

Une donnée expirée = STALE : jamais utilisée pour une NOUVELLE prédiction
lorsque la fraîcheur est requise. Une donnée historique (TTL None) ne
devient JAMAIS stale — mais reste soumise à l'anti-leakage (usable_at).
"""
from sources import registry as _regmod

FRESH = "fresh"
STALE = "stale"
NO_EXPIRY = "no_expiry"
TTL_UNKNOWN = "ttl_unknown"


def ttl_for(data_type, source_id=None, reg=None):
    """TTL secondes depuis le registry réel (None = pas d'expiration)."""
    r = reg or _regmod.load()
    return _regmod.ttl_for(r, data_type, source_id=source_id)


def freshness_status(dp, as_of, source_id=None, reg=None):
    """(status, age_sec, ttl_sec) — status ∈ fresh/stale/no_expiry/ttl_unknown."""
    age = dp.freshness_sec(as_of)
    if age is None:
        return TTL_UNKNOWN, None, None
    ttl = ttl_for(dp.data_type, source_id or dp.source, reg)
    if ttl is None:
        return NO_EXPIRY, age, None
    return (FRESH if age <= ttl else STALE), age, ttl


def is_fresh(dp, as_of, source_id=None, reg=None):
    status, _, _ = freshness_status(dp, as_of, source_id=source_id, reg=reg)
    return status in (FRESH, NO_EXPIRY)


def usable_for_prediction(dp, T, source_id=None, reg=None, require_fresh=True):
    """Utilisable à T ? = ANTI-LEAKAGE (usable_at) [+ fraîcheur si requise].
    require_fresh : données volatiles (live/odds/lineups…) exigent fresh ;
    données historiques (TTL None) passent par définition d'aucune expiration."""
    if not dp.usable_at(T):
        return False
    if not require_fresh:
        return True
    return is_fresh(dp, T, source_id=source_id, reg=reg)
