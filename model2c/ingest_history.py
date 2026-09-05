# -*- coding: utf-8 -*-
"""
INGESTION HISTORIQUE 2C (§3/§30)
=================================
UNIQUEMENT via SafeHttpClient + ComplianceGate (architecture WEB-4).
Aucune requête hors architecture, aucune nouvelle source, aucune
contournement. Toutes les données sont RÉELLES ; l'absence = UNKNOWN.

- OpenLigaDB : 1 requête par ligue-saison (≈20 requêtes au total) ;
- StatsBomb  : competitions + matches + events (réels, couverture PARTIELLE
  assumée) ; un cache FICHIER local hors-ligne évite de retélécharger
  (data/2c_offline — gitignored, aucun secret, données publiques).

Chaque enregistrement canonique porte les horloges PIT :
effective_at (≈ coup de sifflet final) / retrieved_at (récolte).
"""

import json
import os
import time
from datetime import datetime, timezone

from .availability import make_effective_from_kickoff, parse_ts
from .config2c import CONFIG_2C as C
from .identity import canonical_team


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# OpenLigaDB → matchs canoniques
# ---------------------------------------------------------------------------
def parse_ol_season(payload, league, season, retrieved_at):
    """payload : liste JSON OL d'une saison. Ne garde que les matchs FINIS
    avec un Endergebnis (resultTypeID=2). Jamais de score inventé."""
    out = []
    for m in payload:
        if not m.get("matchIsFinished"):
            continue
        ko = m.get("matchDateTimeUTC")
        fin = next((r for r in (m.get("matchResults") or [])
                    if r.get("resultTypeID") == 2), None)
        if ko is None or fin is None:
            continue
        hg, ag = fin.get("pointsTeam1"), fin.get("pointsTeam2")
        if hg is None or ag is None:
            continue
        home = canonical_team((m.get("team1") or {}).get("teamName"))
        away = canonical_team((m.get("team2") or {}).get("teamName"))
        if not home or not away or home == away:
            continue  # identité non résolue → match écarté honnêtement
        out.append({
            "match_id": f"ol:{league}:{m['matchID']}",
            "league": league,
            "season": int(season),
            "kickoff_utc": parse_ts(ko).isoformat(),
            "home": home, "away": away,
            "hg": int(hg), "ag": int(ag),
            "home_name": (m.get("team1") or {}).get("teamName"),
            "away_name": (m.get("team2") or {}).get("teamName"),
            "effective_at": make_effective_from_kickoff(ko, C["match_duration_hours"]),
            "retrieved_at": retrieved_at,
            "source": C["src_openligadb"],
        })
    return out


def fetch_ol_season(client, league, season):
    """1 requête réseau gardée (SafeHttpClient). Retourne matchs canoniques."""
    url = f"https://api.openligadb.de/getmatchdata/{league}/{season}"
    t0 = _now()
    resp = client.fetch(C["src_openligadb"], url, "historical_results")
    payload = json.loads(resp.body.decode("utf-8"))
    return parse_ol_season(payload, league, season, t0)


# ---------------------------------------------------------------------------
# StatsBomb → xG réel (couverture PARTIELLE assumée)
# ---------------------------------------------------------------------------
def _sb_cache_path(offline_dir, kind, key):
    d = os.path.join(offline_dir, "sb_cache", kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{key}.json")


def fetch_sb_json(client, url, data_type, cache_path=None):
    """Fetch gardé + cache fichier local (hors web_cache : artifact offline)."""
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return json.loads(f.read().decode("utf-8")), True
    resp = client.fetch(C["src_statsbomb"], url, data_type)
    payload = json.loads(resp.body.decode("utf-8"))
    if cache_path:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    return payload, False


def fetch_sb_matches(client, competition_id, season_id, offline_dir=None):
    """Liste des matchs SB couverts (peut être PARTIELLE — vérité)."""
    url = (f"https://raw.githubusercontent.com/statsbomb/open-data/master/data/"
           f"matches/{competition_id}/{season_id}.json")
    cp = _sb_cache_path(offline_dir, "matches", f"{competition_id}_{season_id}") if offline_dir else None
    return fetch_sb_json(client, url, "match_calendar", cp)


def aggregate_xg_from_events(events):
    """Somme des statsbomb_xg des tirs par équipe — xG RÉEL match-équipe.
    Retourne {team_name: xg} ; jamais d'estimation."""
    agg = {}
    for e in events or []:
        if e.get("type", {}).get("name") != "Shot":
            continue
        shot = e.get("shot") or {}
        xg = shot.get("statsbomb_xg")
        if xg is None:
            continue
        team = (e.get("team") or {}).get("name")
        if not team:
            continue
        agg[team] = agg.get(team, 0.0) + float(xg)
    return {k: round(v, 4) for k, v in agg.items()}


def fetch_sb_match_xg(client, match_id, offline_dir=None):
    """events/{match_id}.json → {'home_xg','away_xg'} + noms. Une requête
    gardée par match (une seule fois grâce au cache fichier)."""
    url = (f"https://raw.githubusercontent.com/statsbomb/open-data/master/data/"
           f"events/{match_id}.json")
    cp = _sb_cache_path(offline_dir, "events", match_id) if offline_dir else None
    events, _ = fetch_sb_json(client, url, "xg", cp)
    return aggregate_xg_from_events(events)


def build_sb_xg_map(client, competition_id, season_id, offline_dir=None, sleep_s=0.05):
    """xG réel par match couvert. Clé = (canonical_home, canonical_away, date).
    Couverture partielle renvoyée telle quelle — jamais extrapolée."""
    matches, _ = fetch_sb_matches(client, competition_id, season_id, offline_dir)
    xg_map = {}
    meta = {"covered_matches": len(matches), "with_xg": 0, "unmatched_teams": [],
            "errors": [], "interrupted": False}
    for m in matches:
        home = canonical_team((m.get("home_team") or {}).get("home_team_name"))
        away = canonical_team((m.get("away_team") or {}).get("away_team_name"))
        date = m.get("match_date")
        if not home or not away or not date:
            meta["unmatched_teams"].append(((m.get("home_team") or {}).get("home_team_name"),
                                            (m.get("away_team") or {}).get("away_team_name")))
            continue
        try:
            agg = fetch_sb_match_xg(client, m["match_id"], offline_dir)
        except Exception as e:
            # budget SafeHttp atteint : arrêt PROPRE — reprise possible via le
            # cache fichier (les fichiers déjà téléchargés sont réutilisés).
            meta["errors"].append({"match_id": m.get("match_id"),
                                   "error": type(e).__name__})
            if "RateLimit" in type(e).__name__:
                meta["interrupted"] = True
                break
            continue
        hx = agg.get((m.get("home_team") or {}).get("home_team_name"))
        ax = agg.get((m.get("away_team") or {}).get("away_team_name"))
        hx = agg.get((m.get("home_team") or {}).get("home_team_name"))
        ax = agg.get((m.get("away_team") or {}).get("away_team_name"))
        if hx is None or ax is None:
            continue
        xg_map[f"{home}|{away}|{date}"] = (hx, ax)
        meta["with_xg"] += 1
        if sleep_s:
            time.sleep(sleep_s)
    return xg_map, meta
