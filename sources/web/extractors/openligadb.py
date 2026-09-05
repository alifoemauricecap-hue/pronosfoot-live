# -*- coding: utf-8 -*-
"""
OPENLIGADB EXTRACTOR (2B.WEB-3 §9)
====================================
Limité STRICTEMENT aux compétitions couvertes officiellement (Allemagne).
Jamais présenté comme source mondiale ; jamais de remplacement silencieux
d'ESPN : son usage est TOUJOURS explicite (fallback journalisé §23).
"""
from ..normalized import SOURCE_NATIVE
from .base import ensure_obj, point


def parse_matches(obj, ctx, source="openligadb"):
    """getmatchdata → liste de matchs (MatchID, teams, date, résultats)."""
    out, rows = [], []
    data = ensure_obj(obj)
    if not isinstance(data, list):
        return out, rows
    for m in data:
        if not isinstance(m, dict):
            continue
        t1 = (m.get("team1") or {}).get("teamName")
        t2 = (m.get("team2") or {}).get("teamName")
        if not (t1 and t2):
            continue
        dt = m.get("matchDateTimeUTC") or m.get("matchDateTime")
        finished = m.get("matchIsFinished")
        results = m.get("matchResults") or []
        hg = ag = None
        for res in results:
            if res.get("resultTypeID") == 2 or \
                    (res.get("resultName") or "").startswith("Endergebnis"):
                hg, ag = res.get("pointsTeam1"), res.get("pointsTeam2")
        rows.append({"match_id": m.get("matchID"), "date": dt,
                     "home": t1, "away": t2, "finished": finished,
                     "fthg": hg, "ftag": ag,
                     "stats": {"home": {}, "away": {}}, "odds": []})
        mp = point
        now_m = {"match_id": f"openligadb:{m.get('matchID')}"}
        out.append(mp(t1, "home_team", source, ctx, level=SOURCE_NATIVE,
                      effective_at=dt, **now_m))
        out.append(mp(t2, "away_team", source, ctx, level=SOURCE_NATIVE,
                      effective_at=dt, **now_m))
        if dt:
            out.append(mp(dt, "kickoff", source, ctx, level=SOURCE_NATIVE,
                          effective_at=dt, **now_m))
        if finished is not None:
            out.append(mp(bool(finished), "match_completed", source, ctx,
                          level=SOURCE_NATIVE, effective_at=dt, **now_m))
        if hg is not None and ag is not None:
            out.append(mp(hg, "score_home", source, ctx, level=SOURCE_NATIVE,
                          effective_at=dt, **now_m))
            out.append(mp(ag, "score_away", source, ctx, level=SOURCE_NATIVE,
                          effective_at=dt, **now_m))
    return out, rows
