# -*- coding: utf-8 -*-
"""
REJEU FIDÈLE DE LA BASELINE 2A (« poisson 1.0.0 ») SUR DONNÉES HISTORIQUES
===========================================================================
But (D2C-3) : comparer 2A et 2C sur EXACTEMENT les mêmes matchs, en grand N,
sans toucher 2A. Ce module RE-CONSTRUIT les stats d'équipes avec la SEMANTIQUE
EXACTE de app.py::build_stats (fenêtre 150 j, demi-vie 70 j, clamps ligue
[0.9,2.3]/[0.7,2.0]) puis appelle engine.predict_match en IMPORT LECTURE
SEULE (engine.py NON modifié — son hash est surveillé par les tests).

Limites déclarées du rejeu :
- pas de cotes, pas d'absences, pas de compositions (absents de l'historique
  OL) → le rejeu représente la BRANCHE « stats pures » de 2A ;
- les équipes sans aucun historique récent donnent pred=None → NO_PREDICTION
  honnête (comme 2A qui exige des stats minimales).
"""

from datetime import timedelta

import engine  # import LECTURE SEULE — fichier baseline non modifié (2A)
from .availability import parse_ts

WINDOW_DAYS = 150       # engine.MODEL_CONFIG["window_days"]
HALF_LIFE = 70.0        # engine.MODEL_CONFIG["recency_half_life_days"]


def _empty_team():
    return {"sw": 0.0, "n": 0, "gf": 0.0, "ga": 0.0,
            "home_n": 0, "home_sw": 0.0, "home_gf": 0.0, "home_ga": 0.0,
            "away_n": 0, "away_sw": 0.0, "away_gf": 0.0, "away_ga": 0.0,
            "form": []}


def build_stats_at(matches, as_of):
    """Reproduction de app.build_stats à l'instant as_of (fenêtre 150 j).
    matches : matchs TERMINÉS avec kickoff_utc < as_of (déjà filtrés P0).
    Retourne {"teams": ..., "league": ...} au format exact attendu par engine."""
    t0 = parse_ts(as_of)
    lo = t0 - timedelta(days=WINDOW_DAYS)
    teams, lg = {}, {"home_sw": 0.0, "home_gf": 0.0, "away_sw": 0.0, "away_gf": 0.0, "n": 0}
    sel = []
    # les matchs arrivent en ordre chronologique (pipeline) : on remonte la
    # liste et on coupe dès qu'on sort de la fenêtre — même résultat, O(150 j)
    for m in reversed(matches):
        ko = parse_ts(m["kickoff_utc"])
        if ko < lo:
            break
        if ko >= t0:
            continue
        sel.append((ko, m))
    sel.sort(key=lambda x: x[0])
    for ko, m in sel:
        age = (t0.date() - ko.date()).days
        w = 0.5 ** (age / HALF_LIFE)  # sémantique app.weight_for
        h = teams.setdefault(m["home"], _empty_team())
        a = teams.setdefault(m["away"], _empty_team())
        for t, gf_, ga_, loc in ((h, m["hg"], m["ag"], "home"), (a, m["ag"], m["hg"], "away")):
            t["n"] += 1
            t["sw"] += w
            t["gf"] += w * gf_
            t["ga"] += w * ga_
            t[f"{loc}_n"] += 1
            t[f"{loc}_sw"] += w
            t[f"{loc}_gf"] += w * gf_
            t[f"{loc}_ga"] += w * ga_
        h["form"].append({"r": "W" if m["hg"] > m["ag"] else ("D" if m["hg"] == m["ag"] else "L"),
                          "day": ko.date().isoformat()})
        a["form"].append({"r": "W" if m["ag"] > m["hg"] else ("D" if m["ag"] == m["hg"] else "L"),
                          "day": ko.date().isoformat()})
        lg["n"] += 1
        lg["home_sw"] += w
        lg["home_gf"] += w * m["hg"]
        lg["away_sw"] += w
        lg["away_gf"] += w * m["ag"]
    for t in teams.values():
        t["form"].sort(key=lambda x: x["day"], reverse=True)
        t["form"] = t["form"][:6]
    home_avg = min(max((lg["home_gf"] / lg["home_sw"]) if lg["home_sw"] > 1 else 1.45, 0.9), 2.3)
    away_avg = min(max((lg["away_gf"] / lg["away_sw"]) if lg["away_sw"] > 1 else 1.10, 0.7), 2.0)
    return {"teams": teams, "league": {"homeAvg": home_avg, "awayAvg": away_avg, "n": lg["n"]}}


def predict_2a(matches_past, home, away, as_of):
    """Distribution 1N2 (et familles) du rejeu 2A pour un match à as_of.
    matches_past = matchs terminés strictement avant as_of. None si engine
    refuse (stats insuffisantes) — NO_PREDICTION honnête."""
    stats = build_stats_at(matches_past, as_of)
    pred = engine.predict_match(stats, home, away)
    if pred is None:
        return None
    return {"p1": pred["p1"] / 100.0, "px": pred["px"] / 100.0, "p2": pred["p2"] / 100.0,
            "dist": {"1": pred["p1"] / 100.0, "N": pred["px"] / 100.0, "2": pred["p2"] / 100.0},
            "markets": {"1": pred["p1"] / 100.0, "N": pred["px"] / 100.0,
                        "2": pred["p2"] / 100.0,
                        "O1.5": pred["over15"] / 100.0, "O2.5": pred["over25"] / 100.0,
                        "O3.5": pred["over35"] / 100.0,
                        "BTTS_YES": pred["btts"] / 100.0,
                        "BTTS_NO": 1 - pred["btts"] / 100.0},
            "reliab": pred.get("reliab"), "league_n": stats["league"]["n"]}


class OnlineReplay2A:
    """Version incrémentale efficace : maintient l'historique et ne reconstruit
    les stats qu'à la volée (usage pipeline — une passe chronologique)."""

    def __init__(self):
        self.past = []

    def predict_and_update(self, match):
        as_of = match["kickoff_utc"]
        out = predict_2a(self.past, match["home"], match["away"], as_of)
        self.past.append(match)
        return out
