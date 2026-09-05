# -*- coding: utf-8 -*-
"""
AGGREGATION — FORME DES ÉQUIPES (2B.WEB-3 §19)
===============================================
Calcule, UNIQUEMENT depuis des résultats RÉELLEMENT récupérés (rows
normalisées football-data / openligadb / statsbomb / espn historique) :

    forme W/D/L sur 5 ou 10 derniers matchs, buts +/-, clean sheets,
    BTTS, over/under 2.5, domicile vs extérieur, séries.

Niveau = AGGREGATED : derivation_method + model_version + inputs
(identifiants des matchs utilisés). Aucune interpolation : un match sans
score ne compte pas (jourList tronquée honnêtement, pas comblée).

CETTE COUCHE PRÉPARE LES DONNÉES — elle NE MODIFIE PAS le modèle de
prédiction (interdiction §35).
"""
from .normalized import AGGREGATED, MODEL_VERSION, new_datapoint, is_unknown

_METHOD = "team_form_from_real_results(last N, home/away split)"


def team_form(rows, team, last_n=5, venue=None):
    """rows : liste de dicts {date, home, away, fthg, ftag, stats...}
    venue=None|'home'|'away'. Retourne dict de métriques + inputs matchs,
    ou None si AUCUN match exploitable (UNKNOWN en amont)."""
    games = []
    for r in sorted(rows, key=lambda x: (x.get("date") or ""), reverse=True):
        hg, ag = r.get("fthg"), r.get("ftag")
        if hg is None or ag is None:
            continue                              # jamais de score inventé
        if venue == "home" and r.get("home") != team:
            continue
        if venue == "away" and r.get("away") != team:
            continue
        if venue is None and team not in (r.get("home"), r.get("away")):
            continue
        gf = hg if r.get("home") == team else ag
        ga = ag if r.get("home") == team else hg
        games.append({"date": r.get("date"), "gf": gf, "ga": ga,
                      "home_match": r.get("home") == team,
                      "opponent": r.get("away") if r.get("home") == team
                      else r.get("home")})
        if len(games) >= last_n:
            break
    if not games:
        return None
    w = sum(1 for g in games if g["gf"] > g["ga"])
    d = sum(1 for g in games if g["gf"] == g["ga"])
    l = sum(1 for g in games if g["gf"] < g["ga"])
    metrics = {
        "played": len(games), "wins": w, "draws": d, "losses": l,
        "goals_for": sum(g["gf"] for g in games),
        "goals_against": sum(g["ga"] for g in games),
        "clean_sheets": sum(1 for g in games if g["ga"] == 0),
        "btts": sum(1 for g in games if g["gf"] > 0 and g["ga"] > 0),
        "over_2_5": sum(1 for g in games if g["gf"] + g["ga"] >= 3),
        "under_2_5": sum(1 for g in games if g["gf"] + g["ga"] <= 2),
        "form_string": "".join("W" if g["gf"] > g["ga"]
                               else "D" if g["gf"] == g["ga"] else "L"
                               for g in games),
        "current_streak": _streak(games),
        "sample_complete": len(games) >= last_n,
    }
    return {"metrics": metrics, "inputs": [
        {k: g[k] for k in ("date", "opponent", "gf", "ga")} for g in games]}


def _streak(games):
    if not games:
        return None
    first = games[0]
    kind = "W" if first["gf"] > first["ga"] else \
           "D" if first["gf"] == first["ga"] else "L"
    n = 0
    for g in games:
        k = "W" if g["gf"] > g["ga"] else "D" if g["gf"] == g["ga"] else "L"
        if k != kind:
            break
        n += 1
    return f"{kind}x{n}"


def form_datapoints(rows, team, ctx, source, *, last_n=5, venue=None,
                    team_id=None, match_id=None):
    """team_form → DataPoint AGGREGATED (ou UNKNOWN honnête si aucun match)."""
    res = team_form(rows, team, last_n=last_n, venue=venue)
    dt = {"date": None}
    dtype = f"form_last_{last_n}" + (f"_{venue}" if venue else "")
    if res is None:
        from .normalized import unknown_point
        return unknown_point(dtype, source, ctx.retrieved_at,
                             team_id=team_id, match_id=match_id,
                             issues=["NO_COMPLETED_MATCHES_IN_WINDOW"],
                             confidence="unknown")
    return new_datapoint(
        res["metrics"], dtype, source, ctx.retrieved_at, level=AGGREGATED,
        source_url=ctx.source_url, confidence=ctx.confidence,
        team_id=team_id, match_id=match_id,
        derivation_method=_METHOD + f" n={last_n} venue={venue or 'all'}",
        model_version=MODEL_VERSION, inputs=res["inputs"])
