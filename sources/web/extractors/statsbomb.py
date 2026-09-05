# -*- coding: utf-8 -*-
"""
STATSBOMB OPEN DATA EXTRACTOR (2B.WEB-3 §8)
============================================
Licence vérifiée (WEB-0) : usage non-commercial + attribution.

DÉLIMITATION HONNÊTE DES NIVEAUX (§8/§18) :
- `statsbomb_xg` existe PAR TIR dans les événements → chaque tir est
  SOURCE_NATIVE ;
- le xG D'UNE ÉQUIPE sur un match est un CALCUL (somme des tirs) → DERIVED
  avec derivation_method + model_version + inputs ;
- PPDA : calculé UNIQUEMENT si les événements nécessaires sont réellement
  présents (passes adverses dans les 60 % défensifs + actions défensives du
  pressing) ; sinon UNKNOWN explicite — jamais de valeur « à peu près ».
"""
from ..normalized import (SOURCE_NATIVE, DERIVED, MODEL_VERSION,
                          unknown_point)
from .base import ensure_obj, point

_ON_TARGET = ("Goal", "Saved", "Saved to Post")


def parse_competitions(obj, ctx, source="statsbomb_open"):
    """competitions.json → compétitions réellement disponibles."""
    out = []
    data = ensure_obj(obj)
    for c in (data if isinstance(data, list) else []):
        if not isinstance(c, dict):
            continue
        out.append(point({
            "competition_id": c.get("competition_id"),
            "season_id": c.get("season_id"),
            "name": c.get("competition_name"),
            "country": c.get("country_name"),
            "gender": c.get("competition_gender")},
            "competition_available", source, ctx, level=SOURCE_NATIVE))
    return out


def parse_matches(obj, ctx, source="statsbomb_open"):
    out, rows = [], []
    data = ensure_obj(obj)
    for m in (data if isinstance(data, list) else []):
        if not isinstance(m, dict):
            continue
        home = (m.get("home_team") or {}).get("home_team_name")
        away = (m.get("away_team") or {}).get("away_team_name")
        if not (home and away):
            continue
        mid = m.get("match_id")
        rows.append({"match_id": mid, "home": home, "away": away,
                     "date": m.get("match_date"),
                     "kickoff": m.get("kick_off"),
                     "fthg": m.get("home_score"),
                     "ftag": m.get("away_score")})
        ctxm = {"match_id": f"statsbomb:{mid}"}
        out.append(point(home, "home_team", source, ctx,
                         level=SOURCE_NATIVE,
                         effective_at=m.get("match_date"), **ctxm))
        out.append(point(away, "away_team", source, ctx,
                         level=SOURCE_NATIVE,
                         effective_at=m.get("match_date"), **ctxm))
        if m.get("home_score") is not None:
            out.append(point(m["home_score"], "score_home", source, ctx,
                             level=SOURCE_NATIVE,
                             effective_at=m.get("match_date"), **ctxm))
            out.append(point(m.get("away_score"), "score_away", source, ctx,
                             level=SOURCE_NATIVE,
                             effective_at=m.get("match_date"), **ctxm))
    return out, rows


def derive_match_stats(events, ctx, match_date=None, source="statsbomb_open"):
    """Stats dérivées PAR ÉQUIPE depuis events.json d'UN match.

    DERIVED (méthode documentée) : shots, shots_on_target, xg, xga, fouls,
    corners, cards (+ ppda uniquement si données suffisantes)."""
    data = ensure_obj(events)
    if not isinstance(data, list):
        return []
    per_team = {}
    for ev in data:
        if not isinstance(ev, dict):
            continue
        team = (ev.get("possession_team") or {}).get("name") \
            or (ev.get("team") or {}).get("name")
        if not team:
            continue
        acc = per_team.setdefault(team, {
            "shots": 0, "sot": 0, "xg_shots": [], "fouls": 0, "corners": 0,
            "yellow": 0, "red": 0, "loc_total": 0})
        t = (ev.get("type") or {}).get("name")
        if t == "Shot":
            s = ev.get("shot") or {}
            acc["shots"] += 1
            outc = (s.get("outcome") or {}).get("name")
            if outc in _ON_TARGET:
                acc["sot"] += 1
            xg = s.get("statsbomb_xg")
            if isinstance(xg, (int, float)):
                acc["xg_shots"].append(xg)
        elif t == "Foul Committed":
            acc["fouls"] += 1
            card = ((ev.get("foul_committed") or {}).get("card") or {}) \
                .get("name")
            if card == "Yellow Card":
                acc["yellow"] += 1
            elif card in ("Red Card", "Second Yellow"):
                acc["red"] += 1
        elif t == "Pass":
            if ((ev.get("pass") or {}).get("type") or {}).get("name") \
                    == "Corner":
                acc["corners"] += 1
        if ev.get("location"):
            acc["loc_total"] += 1
    out = []
    for team, acc in per_team.items():
        inputs = [f"events:{acc['shots']} shots match_total={len(data)}"]
        def derived(value, dtype, method):
            return point(value, dtype, source, ctx, level=DERIVED,
                         derivation_method=method,
                         model_version=MODEL_VERSION, inputs=inputs,
                         effective_at=match_date, issues=None,
                         team_id=None)
        out.append(derived(acc["shots"], "shots", "count(type=Shot)"))
        out.append(derived(acc["sot"], "shots_on_target",
                           "count(type=Shot & outcome in (Goal,Saved,Saved to Post))"))
        if acc["xg_shots"]:
            out.append(derived(round(sum(acc["xg_shots"]), 3), "xg",
                               "sum(shot.statsbomb_xg) per team"))
        else:
            out.append(unknown_point("xg", source, ctx.retrieved_at,
                                     issues=["NO_XG_IN_EVENTS"]))
        out.append(derived(acc["fouls"], "fouls",
                           "count(type=Foul Committed)"))
        out.append(derived(acc["corners"], "corners",
                           "count(type=Pass & pass.type=Corner)"))
        out.append(derived({"yellow": acc["yellow"], "red": acc["red"]},
                           "cards", "count(foul_committed.card)"))
        # PPDA : exige zones adverses — honnêtement non fiable sans
        # décompte des passes adverses ; UNKNOWN explicite (§8).
        out.append(unknown_point("ppda", source, ctx.retrieved_at,
                                 issues=["PPDA_REQUIRES_OPPOSITION_PASS_MAP"]))
    return out
