# -*- coding: utf-8 -*-
"""
GARDE-FOUS 2C (§19/§20/§21/§23)
================================
- niveaux de données : FULL_DATA / PARTIAL_DATA / LOW_DATA / INSUFFICIENT_DATA ;
- NO_PREDICTION vaut mieux qu'une fausse précision ;
- probabilités extrêmes : bornage + exigence de support de calibration ;
- séparation stricte : probability ≠ confidence ≠ data_quality ≠ sample_size.
"""

from .poisson2c import clamp
from .config2c import CONFIG_2C as C

FULL_DATA = "FULL_DATA"
PARTIAL_DATA = "PARTIAL_DATA"
LOW_DATA = "LOW_DATA"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
NO_PREDICTION = "NO_PREDICTION"


def data_level(home_matches, away_matches):
    """Niveau de données d'un match d'après les matchs antérieurs connus."""
    m = min(home_matches or 0, away_matches or 0)
    if m >= C["full_min_matches"]:
        return FULL_DATA
    if m >= C["partial_min_matches"]:
        return PARTIAL_DATA
    if m >= C["low_min_matches"]:
        return LOW_DATA
    return INSUFFICIENT_DATA


def prediction_allowed(level):
    """INSUFFICIENT_DATA → refus de prédire (NO_PREDICTION, honnêteté §23)."""
    return level != INSUFFICIENT_DATA


def clamp_prob(p):
    return clamp(p, *C["prob_clamp"])


def publish_guard(p, bin_support):
    """Garde-fou anti-extrêmes (§21) : p > extreme_hi (ou < extreme_lo) n'est
    publié tel quel que si le bin de calibration a ≥ extreme_min_support
    observations ; sinon il est ramené vers la borne haute autorisée
    proportionnellement au support réel."""
    p = clamp_prob(p)
    hi, lo = C["extreme_hi"], C["extreme_lo"]
    sup = max(0, bin_support or 0)
    ratio = min(1.0, sup / C["extreme_min_support"])
    if p > hi:
        return hi + (p - hi) * ratio
    if p < lo:
        return lo - (lo - p) * ratio
    return p


def metric_verdict(n, value=None):
    """Une métrique sans N suffisant n'est pas une conclusion (§19)."""
    if n is None or n < C["minimum_sample_size"]:
        return "INSUFFICIENT_SAMPLE"
    if n < C["strong_claim_n"]:
        return "WEAK_SAMPLE"
    return "OK"


class DataQuality:
    """Qualité de DONNÉE (§22) — N'EST PAS une probabilité de victoire."""

    def __init__(self, level, n_home, n_away, xg_real, sources):
        self.level = level
        self.score = self._score(level, n_home, n_away, xg_real)
        self.sources = sorted(sources)

    @staticmethod
    def _score(level, n_home, n_away, xg_real):
        base = {FULL_DATA: 0.9, PARTIAL_DATA: 0.65, LOW_DATA: 0.4,
                INSUFFICIENT_DATA: 0.1}[level]
        depth = min(1.0, ((n_home or 0) + (n_away or 0)) / 40.0)
        s = 0.7 * base + 0.3 * depth
        if xg_real:
            s = min(1.0, s + 0.05)
        return round(s, 4)

    def as_dict(self):
        return {"level": self.level, "score": self.score, "sources": self.sources,
                "label": "DATA_QUALITY — qualité de la DONNÉE, PAS une probabilité"}
