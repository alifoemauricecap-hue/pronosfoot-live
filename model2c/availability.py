# -*- coding: utf-8 -*-
"""
ANTI-LEAKAGE — PRIORITÉ P0 ABSOLUE (§2)
========================================
Porte centrale : is_available_at(datapoint, T, mode).

Deux horloges, deux modes — TOUJOURS documentés :

- mode "live" (production/shadow) : une donnée est utilisable à T ssi
      effective_at <= T  ET  retrieved_at <= T
  (nous ne pouvons utiliser que ce que NOUS avions réellement recueilli).

- mode "backtest" (reconstitution historique) : une donnée est utilisable à T ssi
      effective_at <= T
  L'heure retrieved_at d'un FICHIER HISTORIQUE (p.ex. une saison OpenLigaDB
  téléchargée aujourd'hui) est un artefact de notre récolte — le RÉSULTAT
  d'un match de 2021 était publiquement disponible dès son coup de sifflet
  final (effective_at). retrieved_at n'est donc pas exigé en backtest ;
  effective_at reste STRICTEMENT exigé. Cette distinction est assumée et
  testée : une donnée FUTURE (effective_at > T) est rejetée dans les DEUX modes.

Une donnée sans horloge exploitable est REFUSÉE (UNKNOWN — jamais supposée).
"""

from datetime import datetime, timezone

LIVE = "live"
BACKTEST = "backtest"


def parse_ts(value):
    """ISO 8601 → datetime aware UTC. Accepte 'Z'. None/vide → None.
    Ne devine jamais : une chaîne invalide lève ValueError."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clock(dp, key):
    v = dp.get(key)
    return parse_ts(v) if v is not None else None


def is_available_at(datapoint, T, mode=LIVE):
    """True ssi `datapoint` était utilisable au moment T, selon `mode`.

    Règles :
    - effective_at est TOUJOURS exigé (≤ T) dans les deux modes ;
    - retrieved_at est exigé (≤ T) en mode "live" uniquement ;
    - horloge manquante ⇒ REFUS (False) — UNKNOWN, jamais supposé ;
    - T peut être datetime ou ISO str.
    """
    if mode not in (LIVE, BACKTEST):
        raise ValueError(f"mode inconnu : {mode}")
    t = parse_ts(T)
    if t is None:
        raise ValueError("T invalide")
    eff = _clock(datapoint, "effective_at")
    if eff is None or eff > t:
        return False
    if mode == LIVE:
        ret = _clock(datapoint, "retrieved_at")
        if ret is None or ret > t:
            return False
    return True


def available_points(datapoints, T, mode=LIVE):
    """Filtre une liste de datapoints — n'en garde que les utilisables à T."""
    return [dp for dp in datapoints if is_available_at(dp, T, mode)]


def assert_no_future(datapoints, T, mode=LIVE):
    """Garde-fou de pipeline : lève AssertionError si UNE donnée future passe.
    Utilisé dans les tests et les chemins critiques (extra-prudence P0)."""
    t = parse_ts(T)
    bad = [dp for dp in datapoints if not is_available_at(dp, t, mode)]
    assert not bad, f"FUTURE DATA LEAK : {len(bad)} datapoint(s) postérieur(s) à {t.isoformat()}"
    return True


def make_effective_from_kickoff(kickoff_utc, duration_hours):
    """Hypothèse D2C-E : le résultat d'un match devient disponible à
    kickoff + duration (≈ coup de sifflet final)."""
    from datetime import timedelta
    ko = parse_ts(kickoff_utc)
    return (ko + timedelta(hours=float(duration_hours))).isoformat()
