# -*- coding: utf-8 -*-
"""
ESPN EXTRACTOR (2B.WEB-3 §5)
=============================
Extraction fidèle des structures RÉELLES ESPN (site.api.espn.com) :
scoreboard (events→competitions→competitors/status/venue/odds/details) et
summary (boxscore.teams.statistics, rosters, injuries, details, odds).

JAMAIS de prétention : si une section n'est pas dans la réponse, elle n'est
PAS émise (aucune composition/blessure/cote inventée).

Statistiques boxscore : mapping ALIGNÉ sur le live historique (app.py
STAT_LABELS) — réutilisé tel quel pour cohérence.
"""
from .base import ensure_obj, point
from ..normalized import SOURCE_NATIVE

_STAT_LABELS = {"Possession": "possession", "Shots": "shots",
                "On Goal": "shots_on_target", "Corner Kicks": "corners",
                "Fouls": "fouls", "Yellow Cards": "cards_yellow",
                "Red Cards": "cards_red"}


def _competitors(event):
    comps = (event.get("competitions") or [{}])
    return comps[0].get("competitors") or comps[0], comps[0]


def _home_away(event):
    _, comp0 = _competitors(event)
    competitors = comp0.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    return home, away


def _score(c):
    s = c.get("score")
    try:
        if isinstance(s, dict):
            return int(s.get("value"))
        return int(s)
    except (TypeError, ValueError):
        return None


def parse_event_identity(event, ctx, source="espn"):
    """MATCH : match_id, équipes, compétition, date, kickoff, venue, status."""
    out = []
    cid = event.get("id")
    if cid:
        out.append(point(f"espn:{ctx.extras.get('league_code', 'x')}:{cid}",
                         "match_ref", source, ctx, level=SOURCE_NATIVE,
                         effective_at=event.get("date"), match_id=None))
    lg = event.get("name")
    if lg:
        out.append(point(lg, "match_label", source, ctx, level=SOURCE_NATIVE))
    home, away = _home_away(event)
    if home and home.get("team", {}).get("displayName"):
        out.append(point(home["team"]["displayName"], "home_team", source,
                         ctx, level=SOURCE_NATIVE,
                         team_id=home["team"].get("id")))
    if away and away.get("team", {}).get("displayName"):
        out.append(point(away["team"]["displayName"], "away_team", source,
                         ctx, level=SOURCE_NATIVE,
                         team_id=away["team"].get("id")))
    lg0 = ctx.extras.get("league_name")
    if lg0:
        out.append(point(lg0, "competition", source, ctx, level=SOURCE_NATIVE))
    if event.get("date"):
        out.append(point(event["date"], "kickoff", source, ctx,
                         level=SOURCE_NATIVE, effective_at=event["date"]))
    _, comp0 = _competitors(event)
    venue = comp0.get("venue") or event.get("venue") or {}
    if venue.get("fullName"):
        out.append(point(venue["fullName"], "venue", source, ctx,
                         level=SOURCE_NATIVE))
        addr = venue.get("address") or {}
        if addr.get("city"):
            out.append(point(addr["city"], "venue_city", source, ctx,
                             level=SOURCE_NATIVE))
        if addr.get("country"):
            out.append(point(addr["country"], "venue_country", source, ctx,
                             level=SOURCE_NATIVE))
    st = (comp0.get("status") or event.get("status") or {}).get("type") or {}
    if st.get("name"):
        out.append(point(st["name"], "match_status", source, ctx,
                         level=SOURCE_NATIVE))
    if isinstance(st.get("completed"), bool):
        out.append(point(st["completed"], "match_completed", source, ctx,
                         level=SOURCE_NATIVE))
    return out


def parse_event_live(event, ctx, source="espn"):
    """LIVE : score, minute/clock, période, événements (details)."""
    out = []
    home, away = _home_away(event)
    hs, as_ = (_score(home) if home else None), (_score(away) if away else None)
    if hs is not None:
        out.append(point(hs, "score_home", source, ctx, level=SOURCE_NATIVE))
    if as_ is not None:
        out.append(point(as_, "score_away", source, ctx, level=SOURCE_NATIVE))
    _, comp0 = _competitors(event)
    st = (comp0.get("status") or event.get("status") or {})
    stt = st.get("type") or {}
    if stt.get("shortDetail") or stt.get("detail"):
        out.append(point(stt.get("shortDetail") or stt.get("detail"),
                         "clock", source, ctx, level=SOURCE_NATIVE))
    minutes = st.get("displayClock")
    if minutes is not None:
        out.append(point(str(minutes), "display_clock", source, ctx,
                         level=SOURCE_NATIVE))
    if st.get("period") is not None:
        out.append(point(st["period"], "period", source, ctx,
                         level=SOURCE_NATIVE))
    # FORM (lettres « WWDLW » fournies par ESPN par concurrent)
    for c, canon in ((home, "form_home"), (away, "form_away")):
        f = (c or {}).get("form")
        if isinstance(f, str) and f:
            out.append(point(f, canon, source, ctx, level=SOURCE_NATIVE,
                             team_id=(c or {}).get("team", {}).get("id")))
    # EVENTS (details) — buts / cartons / changements tels que fournis
    details = comp0.get("details") or []
    events = []
    for d in details:
        if not isinstance(d, dict):
            continue
        events.append({
            "type": (d.get("type") or {}).get("text"),
            "clock": (d.get("clock") or {}).get("displayValue"),
            "team": ((d.get("team") or {}).get("displayName")),
            "athlete": ((d.get("athletesInvolved") or [{}])[0] or {})
                       .get("displayName"),
        })
    if events:
        out.append(point(events, "match_events", source, ctx,
                         level=SOURCE_NATIVE))
    # ODDS réellement présentes dans la réponse (marché + bookmaker)
    odds = comp0.get("odds") or []
    parsed = []
    for o in odds:
        if not isinstance(o, dict):
            continue
        entry = {"bookmaker": (o.get("provider") or {}).get("name"),
                 "details": o.get("details"),
                 "over_under": o.get("overUnder"),
                 "moneyline_home": (o.get("homeTeamOdds") or {})
                 .get("moneyLine"),
                 "moneyline_away": (o.get("awayTeamOdds") or {})
                 .get("moneyLine"),
                 "spread": o.get("spread")}
        if any(v is not None for k, v in entry.items() if k != "bookmaker"):
            parsed.append(entry)
    if parsed:
        out.append(point(parsed, "odds", source, ctx, level=SOURCE_NATIVE))
    return out


def parse_summary_stats(summary, ctx, source="espn"):
    """STATS match (boxscore.teams[].statistics) : possessions, tirs,
    tirs cadrés, corners, fautes, cartons — mapping du live historique."""
    out = []
    teams = ((summary.get("boxscore") or {}).get("teams")) or []
    for t in teams:
        team = (t.get("team") or {})
        tid = team.get("id")
        stats = t.get("statistics") or []
        raw = {}
        for s in stats:
            if isinstance(s, dict) and s.get("name") is not None:
                raw[s.get("name")] = s.get("displayValue")
        for label, dtype in _STAT_LABELS.items():
            if label in raw:
                val = raw[label]
                if label == "Possession":
                    try:
                        val = float(str(val).replace("%", ""))
                    except ValueError:
                        pass
                else:
                    try:
                        val = int(str(val).split(".")[0])
                    except ValueError:
                        pass
                out.append(point(val, dtype, source, ctx,
                                 level=SOURCE_NATIVE, team_id=tid))
    return out


def parse_summary_lineups(summary, ctx, source="espn"):
    """LINEUPS (boxscore players / rosters) — émises UNIQUEMENT si présentes."""
    out = []
    teams = ((summary.get("boxscore") or {}).get("players")) or []
    for t in teams:
        team = (t.get("team") or {})
        roster = []
        stats_groups = t.get("statistics") or []
        for g in stats_groups:
            for a in (g.get("athletes") or []):
                ath = (a.get("athlete") or {})
                if not ath.get("displayName"):
                    continue
                roster.append({
                    "player": ath["displayName"],
                    "starter": bool(a.get("starter")),
                    "position": ((ath.get("position") or {}).get("abbreviation")),
                })
        if roster:
            out.append(point(roster, "lineup", source, ctx,
                             level=SOURCE_NATIVE, team_id=team.get("id")))
    return out


def parse_summary_injuries(summary, ctx, source="espn"):
    """INJURIES — section injuries[] du summary, uniquement si présente."""
    out = []
    for t in (summary.get("injuries") or []):
        team = (t.get("team") or {})
        rows = []
        for inj in (t.get("injuries") or []):
            if not isinstance(inj, dict):
                continue
            ath = (inj.get("athlete") or {})
            rows.append({"player": ath.get("displayName"),
                         "status": inj.get("status"),
                         "detail": inj.get("shortComment") or inj.get("type")
                         if isinstance(inj.get("type"), str) else
                         (inj.get("type") or {}).get("description")})
        if rows:
            out.append(point(rows, "injuries", source, ctx,
                             level=SOURCE_NATIVE, team_id=team.get("id")))
    return out


def parse_standings(standings_obj, ctx, source="espn"):
    """CLASSEMENTS : groupes → équipes (position, points si fournis)."""
    out = []
    groups = standings_obj.get("children") or [standings_obj]
    for g in groups:
        for s in (g.get("standings") or {}).get("entries") or []:
            team = (s.get("team") or {})
            stats = {st.get("name"): st.get("displayValue")
                     for st in (s.get("stats") or []) if isinstance(st, dict)}
            value = {"team": team.get("displayName"),
                     "rank": stats.get("rank") or stats.get("gamesPlayed")
                     and stats.get("rank"),
                     "points": stats.get("points")}
            if value["team"]:
                out.append(point(value, "standings_row", source, ctx,
                                 level=SOURCE_NATIVE, team_id=team.get("id")))
    return out
