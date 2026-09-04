# -*- coding: utf-8 -*-
"""
⚽ PronoFoot Live 3.0 — Application de pronostics football TEMPS RÉEL
   ÉTAPE 2A « SOCLE DE VÉRITÉ » : persistance SQLite (WAL) + migrations,
   prédictions GELÉES append-only (SHA-256, trigger SQL d'immuabilité),
   anti-fuite (aucune prédiction après le coup d'envoi ; match terminé sans
   gel pré-match = NOT_EVALUABLE ; l'ancien recalcul post-match noté comme
   prédiction a été SUPPRIMÉ), métriques propres Brier/LogLoss publiées
   seulement sur échantillon propre suffisant, sauvegarde GitHub Release.
======================================================================
- Flux SSE (Server-Sent Events) : les scores/stats sont POUSSÉS vers le
  navigateur toutes les ~15 secondes pendant les matchs (comme Flashscore)
- ~76 compétitions mondiales (données publiques ESPN, sans clé)
- Moteur IA : Poisson pondéré par récence + momentum live (tirs, possession,
  corners) + fusion des cotes réelles des bookmakers quand disponibles
- Analyse d'expert en français générée automatiquement : forme, forces
  dom/ext, H2H, absences/blessures, momentum → "PARI LE PLUS SÛR"
- Blessures, compositions, stats live, chronologie (buts/cartons/rempl.)

Note : l'API publique ESPN refuse les User-Agent "Mozilla" (403).
"""

import json
import math
import os
import queue
import threading
import time
import urllib.request
import gzip
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request, Response, send_from_directory

import db as db_layer
import repository as repo
import prediction_service as predsvc
import backup
from engine import MODEL_NAME, MODEL_VERSION, MODEL_CONFIG, american_to_prob

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={rng}"
ESPN_STANDINGS  = "https://site.web.api.espn.com/apis/v2/sports/soccer/{code}/standings"
ESPN_SUMMARY    = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/summary?event={eid}"

STATS_DAYS = 150
FEED_PAST, FEED_AHEAD = 1, 8
LIVE_INTERVAL = 25      # secondes entre deux boucles live
FEED_INTERVAL_IDLE = 120

def cdn_small(url, size=80):
    """Petites vignettes de logos (gain de data massif sur mobile 3G)."""
    if not url or "a.espncdn.com" not in url:
        return url
    path = url.split("a.espncdn.com")[-1]
    return f"https://a.espncdn.com/combiner/i?img={path}&w={size}&h={size}"

GROUPS = {
    "europe":        {"label": "Europe",            "icon": "🇪🇺"},
    "ameriques":     {"label": "Amériques",         "icon": "🌎"},
    "afrique-asie":  {"label": "Afrique & Asie",    "icon": "🌍"},
    "international": {"label": "Sélections & Monde","icon": "🏆"},
}

def L(code, name, flag, group, sname=None):
    return {"code": code, "name": name, "flag": flag, "group": group, "sname": sname or name}

LEAGUES = [
    L("eng.1","Premier League","🏴","europe","Angleterre 1"), L("eng.2","Championship","🏴","europe","Angleterre 2"),
    L("eng.3","League One","🏴","europe","Angleterre 3"), L("eng.fa","FA Cup","🏴","europe","Coupe d'Angleterre"),
    L("eng.league_cup","Carabao Cup","🏴","europe","Coupe de la Ligue Anglaise"),
    L("esp.1","LaLiga","🇪🇸","europe","Espagne 1"), L("esp.2","LaLiga 2","🇪🇸","europe","Espagne 2"),
    L("esp.copa_del_rey","Copa del Rey","🇪🇸","europe","Coupe d'Espagne"),
    L("ger.1","Bundesliga","🇩🇪","europe","Allemagne 1"), L("ger.2","2. Bundesliga","🇩🇪","europe","Allemagne 2"),
    L("ger.dfb_pokal","DFB-Pokal","🇩🇪","europe","Coupe d'Allemagne"),
    L("ita.1","Serie A","🇮🇹","europe","Italie 1"), L("ita.2","Serie B","🇮🇹","europe","Italie 2"),
    L("ita.coppa_italia","Coppa Italia","🇮🇹","europe","Coupe d'Italie"),
    L("fra.1","Ligue 1","🇫🇷","europe","France 1"), L("fra.2","Ligue 2","🇫🇷","europe","France 2"),
    L("fra.coupe_de_france","Coupe de France","🇫🇷","europe"),
    L("por.1","Liga Portugal","🇵🇹","europe","Portugal 1"), L("ned.1","Eredivisie","🇳🇱","europe","Pays-Bas 1"),
    L("bel.1","Pro League","🇧🇪","europe","Belgique 1"), L("tur.1","Süper Lig","🇹🇷","europe","Turquie 1"),
    L("sco.1","Scottish Premiership","🏴","europe","Écosse 1"), L("rus.1","Premier League Russie","🇷🇺","europe","Russie 1"),
    L("gre.1","Super League Grèce","🇬🇷","europe","Grèce 1"), L("aut.1","Bundesliga Autriche","🇦🇹","europe","Autriche 1"),
    L("sui.1","Super League Suisse","🇨🇭","europe","Suisse 1"), L("den.1","Superliga Danemark","🇩🇰","europe","Danemark 1"),
    L("nor.1","Eliteserien","🇳🇴","europe","Norvège 1"), L("swe.1","Allsvenskan","🇸🇪","europe","Suède 1"),
    L("cze.1","Fortuna Liga Tchèque","🇨🇿","europe","Tchéquie 1"), L("isr.1","Ligat ha'Al","🇮🇱","europe","Israël 1"),
    L("cyp.1","First Division Chypre","🇨🇾","europe","Chypre 1"), L("rou.1","Liga 1 Roumanie","🇷🇴","europe","Roumanie 1"),
    L("uefa.champions","Ligue des Champions","🇪🇺","europe","LDC"), L("uefa.europa","Ligue Europa","🇪🇺","europe","UEL"),
    L("uefa.super_cup","Supercoupe UEFA","🇪🇺","europe"),
    L("usa.1","MLS","🇺🇸","ameriques"), L("usa.nwsl","NWSL (F)","🇺🇸","ameriques"), L("mex.1","Liga MX","🇲🇽","ameriques"),
    L("bra.1","Brasileirão Série A","🇧🇷","ameriques","Brésil 1"), L("bra.2","Brasileirão Série B","🇧🇷","ameriques","Brésil 2"),
    L("arg.1","Primera División","🇦🇷","ameriques","Argentine 1"), L("arg.2","Nacional B","🇦🇷","ameriques","Argentine 2"),
    L("col.1","Primera A","🇨🇴","ameriques","Colombie 1"), L("chi.1","Primera División","🇨🇱","ameriques","Chili 1"),
    L("ecu.1","LigaPro","🇪🇨","ameriques","Équateur 1"), L("per.1","Liga 1","🇵🇪","ameriques","Pérou 1"),
    L("uru.1","Primera División","🇺🇾","ameriques","Uruguay 1"), L("par.1","Primera División","🇵🇾","ameriques","Paraguay 1"),
    L("ven.1","Primera División","🇻🇪","ameriques","Venezuela 1"), L("bol.1","División Profesional","🇧🇴","ameriques","Bolivie 1"),
    L("conmebol.libertadores","Copa Libertadores","🌎","ameriques"), L("conmebol.sudamericana","Copa Sudamericana","🌎","ameriques"),
    L("concacaf.champions","Concacaf Champions Cup","🌎","ameriques"),
    L("caf.champions","Ligue des Champions CAF","🌍","afrique-asie","CAF LDC"), L("caf.nations","Coupe d'Afrique des Nations","🌍","afrique-asie","CAN"),
    L("afc.champions","AFC Champions League","🌏","afrique-asie","AFC CL"), L("afc.cup","AFC Champions League 2","🌏","afrique-asie","AFC CL2"),
    L("ksa.1","Saudi Pro League","🇸🇦","afrique-asie","Arabie Saoudite 1"), L("jpn.1","J1 League","🇯🇵","afrique-asie","Japon 1"),
    L("chn.1","Super League Chine","🇨🇳","afrique-asie","Chine 1"), L("ind.1","Indian Super League","🇮🇳","afrique-asie","Inde 1"),
    L("aus.1","A-League","🇦🇺","afrique-asie","Australie 1"), L("rsa.1","Betway Premiership","🇿🇦","afrique-asie","Afrique du Sud 1"),
    L("nga.1","NPFL","🇳🇬","afrique-asie","Nigeria 1"),
    L("fifa.world","Coupe du Monde","🏆","international"), L("fifa.cwc","Coupe du Monde des Clubs","🌍","international","CWC"),
    L("fifa.friendly","Matchs Amicaux Internationaux","🌍","international","Amicaux"),
    L("fifa.worldq.uefa","Qualif. Mondial (UEFA)","🇪🇺","international"), L("fifa.worldq.caf","Qualif. Mondial (CAF)","🌍","international"),
    L("fifa.worldq.concacaf","Qualif. Mondial (Concacaf)","🌎","international"), L("fifa.worldq.conmebol","Qualif. Mondial (CONMEBOL)","🌎","international"),
    L("fifa.worldq.afc","Qualif. Mondial (AFC)","🌏","international"),
    L("uefa.nations","Ligue des Nations","🇪🇺","international"), L("uefa.euro","Euro","🇪🇺","international"),
    L("concacaf.gold","Gold Cup","🌎","international"),
]
LEAGUE_BY_CODE = {lg["code"]: lg for lg in LEAGUES}

# ---------------------------------------------------------------------------
# Cache + HTTP + utilitaires
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and item[0] > time.time():
            return item[1]
    return None

def cache_set(key, value, ttl):
    with _cache_lock:
        _cache[key] = (time.time() + ttl, value)

def http_json(url, retries=2, timeout=15):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(0.3 * (i + 1))
    raise last

def dstr(d): return d.strftime("%Y%m%d")

def date_chunks(start_date, end_date, chunk_days):
    chunks, d = [], start_date
    while d <= end_date:
        e = min(d + timedelta(days=chunk_days - 1), end_date)
        chunks.append(dstr(d) if d == e else f"{dstr(d)}-{dstr(e)}")
        d = e + timedelta(days=1)
    return chunks

def to_int(x, default=0):
    try: return int(float(x))
    except Exception: return default

def to_float(x, default=0.0):
    try: return float(x)
    except Exception: return default

# ---------------------------------------------------------------------------
# Événements (scoreboard)
# ---------------------------------------------------------------------------
def _ht_score(c):
    """Score à la mi-temps si ESPN le fournit (linescores, période 1) — §3 RESULTS."""
    try:
        for ls in (c.get("linescores") or []):
            if int(ls.get("period", 0)) == 1:
                v = ls.get("value")
                if v is None:
                    v = ls.get("displayValue")
                return to_int(v, None)
    except Exception:
        return None
    return None

def parse_event(e, lmeta):
    try:
        comp = e["competitions"][0]
        st = e["status"]["type"]
        home = away = None
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            node = {"id": t.get("id"), "name": t.get("shortDisplayName") or t.get("displayName") or t.get("name"),
                    "full": t.get("displayName") or t.get("name"), "logo": cdn_small(t.get("logo")),
                    "score": c.get("score"), "winner": c.get("winner", False), "shootout": c.get("shootoutScore"),
                    "ht": _ht_score(c)}
            if c.get("homeAway") == "home": home = node
            else: away = node
        if not home or not away or not home["id"] or not away["id"]:
            return None
        if (home["name"] or "").upper() in ("TBD", "TBA") or (away["name"] or "").upper() in ("TBD", "TBA"):
            return None
        return {"id": e["id"], "utc": e["date"], "day": e["date"][:10],
                "state": st.get("state"), "detail": st.get("shortDetail") or st.get("detail") or "",
                "clock": e["status"].get("displayClock", ""), "clockSec": e["status"].get("clock", 0) or 0,
                "completed": st.get("completed", False), "home": home, "away": away,
                "venue": (comp.get("venue") or {}).get("fullName", "")}
    except Exception:
        return None

def fetch_league_window(code, chunks, ttl=60):
    cache_key = f"win:{code}:{chunks[0]}:{chunks[-1]}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    lmeta = LEAGUE_BY_CODE.get(code, {"code": code, "name": code, "flag": "⚽", "group": "europe"})
    urls = [ESPN_SCOREBOARD.format(code=code, rng=rng) for rng in chunks]
    events, league_logo = {}, None
    def one(u):
        try: return http_json(u, retries=1)
        except Exception: return {}
    with ThreadPoolExecutor(max_workers=min(5 if ttl > 3600 else 8, len(urls))) as ex:
        for d in ex.map(one, urls):
            if not isinstance(d, dict): continue
            if not league_logo and d.get("leagues"):
                logos = d["leagues"][0].get("logos") or []
                if logos: league_logo = cdn_small(logos[0].get("href"))
            for e in d.get("events", []):
                p = parse_event(e, lmeta)
                if p: events[p["id"]] = p
    # FILTRE STRICT : ne garder que les matchs réellement programmés dans la
    # fenêtre demandée (l'API renvoie parfois des événements hors-plage)
    w_start = int(chunks[0][:8]); w_end = int(chunks[-1][-8:])
    events = {k: v for k, v in events.items() if w_start <= int(v["day"].replace("-", "")) <= w_end}
    result = {"events": sorted(events.values(), key=lambda x: x["utc"]),
              "leagueLogo": league_logo, "fetchedAt": time.time()}
    cache_set(cache_key, result, ttl)
    return result

# ---------------------------------------------------------------------------
# Statistiques d'équipes (150 jours, pondération par récence)
# ---------------------------------------------------------------------------
def empty_team(tid, name="", logo=None):
    return {"id": tid, "name": name, "logo": logo, "n": 0, "sw": 0.0, "gf": 0.0, "ga": 0.0,
            "home_n": 0, "home_sw": 0.0, "home_gf": 0.0, "home_ga": 0.0,
            "away_n": 0, "away_sw": 0.0, "away_gf": 0.0, "away_ga": 0.0,
            "w": 0, "d": 0, "l": 0, "pts": 0, "form": [], "matches": []}

def weight_for(day_str):
    try:
        dt = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age = max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        age = 150
    return 0.5 ** (age / 70.0)

def build_stats(code):
    skey = f"stats:{code}"
    cached = cache_get(skey)
    if cached is not None:
        return cached
    today = datetime.now(timezone.utc).date()
    chunks = date_chunks(today - timedelta(days=STATS_DAYS), today - timedelta(days=1), 13)
    data = fetch_league_window(code, chunks, ttl=6 * 3600)
    teams, lg = {}, {"home_sw": 0.0, "home_gf": 0.0, "away_sw": 0.0, "away_gf": 0.0, "n": 0}
    for ev in data["events"]:
        if ev["state"] != "post" or ev["home"]["score"] is None or ev["away"]["score"] is None:
            continue
        hg, ag = to_int(ev["home"]["score"], -1), to_int(ev["away"]["score"], -1)
        if hg < 0 or ag < 0: continue
        w = weight_for(ev["day"])
        h = teams.setdefault(ev["home"]["id"], empty_team(ev["home"]["id"], ev["home"]["name"], ev["home"]["logo"]))
        a = teams.setdefault(ev["away"]["id"], empty_team(ev["away"]["id"], ev["away"]["name"], ev["away"]["logo"]))
        h["name"], h["logo"] = ev["home"]["name"], ev["home"]["logo"] or h["logo"]
        a["name"], a["logo"] = ev["away"]["name"], ev["away"]["logo"] or a["logo"]
        for t, gf_, ga_, loc in ((h, hg, ag, "home"), (a, ag, hg, "away")):
            t["n"] += 1; t["sw"] += w; t["gf"] += w * gf_; t["ga"] += w * ga_
            t[f"{loc}_n"] += 1; t[f"{loc}_sw"] += w; t[f"{loc}_gf"] += w * gf_; t[f"{loc}_ga"] += w * ga_
        if hg > ag:   h["w"] += 1; h["pts"] += 3; a["l"] += 1
        elif hg < ag: a["w"] += 1; a["pts"] += 3; h["l"] += 1
        else: h["d"] += 1; a["d"] += 1; h["pts"] += 1; a["pts"] += 1
        h["form"].append({"r": "W" if hg > ag else ("D" if hg == ag else "L"), "gf": hg, "ga": ag,
                          "day": ev["day"], "loc": "D", "opp": ev["away"]["full"]})
        a["form"].append({"r": "W" if ag > hg else ("D" if ag == hg else "L"), "gf": ag, "ga": hg,
                          "day": ev["day"], "loc": "E", "opp": ev["home"]["full"]})
        mrec = {"day": ev["day"], "home": ev["home"]["full"], "away": ev["away"]["full"],
                "homeId": ev["home"]["id"], "awayId": ev["away"]["id"], "hg": hg, "ag": ag}
        h["matches"].append(mrec); a["matches"].append(mrec)
        lg["n"] += 1; lg["home_sw"] += w; lg["home_gf"] += w * hg; lg["away_sw"] += w; lg["away_gf"] += w * ag
    for t in teams.values():
        t["form"].sort(key=lambda x: x["day"], reverse=True); t["form"] = t["form"][:6]
        t["matches"].sort(key=lambda x: x["day"], reverse=True)
    out = {"teams": teams,
           "league": {"n": lg["n"],
                      "homeAvg": min(max((lg["home_gf"] / lg["home_sw"]) if lg["home_sw"] > 1 else 1.45, 0.9), 2.3),
                      "awayAvg": min(max((lg["away_gf"] / lg["away_sw"]) if lg["away_sw"] > 1 else 1.10, 0.7), 2.0)}}
    cache_set(skey, out, 6 * 3600)
    persist_cache(skey, out, 6 * 3600)   # §18 : redémarrage rapide depuis la persistance
    return out

_stats_locks = {}
_stats_building = set()

def ensure_stats_async(code):
    if cache_get(f"stats:{code}") is not None: return
    with _stats_locks.setdefault(code, threading.Lock()):
        if code in _stats_building or cache_get(f"stats:{code}") is not None: return
        _stats_building.add(code)
        def job():
            try: build_stats(code)
            except Exception as ex: print(f"[stats] {code}: {ex}")
            finally: _stats_building.discard(code)
        threading.Thread(target=job, daemon=True).start()

# ---------------------------------------------------------------------------
# MOTEUR — déplacé dans engine.py (importé en tête de fichier, code IDENTIQUE)
# Évaluation honnête : settle_family / multiclass_brier / multiclass_logloss.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# PARSING SUMMARY (stats live, timeline, compos, blessés, cotes)
# ---------------------------------------------------------------------------
STAT_LABELS = {"Possession": "poss", "Shots": "shots", "On Goal": "sot", "Corner Kicks": "corners",
               "Fouls": "fouls", "Yellow Cards": "yellow", "Red Cards": "red", "Saves": "saves",
               "Offsides": "offsides"}

def parse_summary(data):
    """Extrait tout le nécessaire d'un résumé ESPN (live, pre ou post)."""
    out = {"liveStats": None, "timeline": [], "injuries": {"home": [], "away": []},
           "lineups": None, "odds_probs": None, "seasonStats": None,
           "odds_provider": None, "odds_dec": None}
    bs = data.get("boxscore", {}) or {}
    teams = bs.get("teams", []) or []
    # ---- Stats d'équipe (live ou agrégats saison) ----
    stat_map = []
    for t in teams[:2]:
        row = {"name": t.get("team", {}).get("displayName"), "home": (t.get("homeAway") == "home")}
        for s in t.get("statistics", []) or []:
            label = s.get("label") or s.get("name")
            key = STAT_LABELS.get(label) or (("sot" if label == "ON GOAL" else None) or STAT_LABELS.get((label or "").title()))
            if s.get("label") == "ON GOAL": key = "sot"
            if s.get("label") == "SHOTS": key = "shots"
            if key:
                row[key] = to_float(s.get("displayValue"), 0)
        stat_map.append(row)
    if len(stat_map) == 2 and any("poss" in r or "shots" in r for r in stat_map):
        out["liveStats"] = stat_map
    elif len(stat_map) == 2 and teams:
        # agrégats saison (avant-match)
        season = []
        for t in teams[:2]:
            season.append({"name": t.get("team", {}).get("displayName"), "home": t.get("homeAway") == "home",
                           "stats": [{"label": s.get("label"), "v": s.get("displayValue")} for s in t.get("statistics", []) or []][:8]})
        out["seasonStats"] = season
    # ---- Blessés / absents ----
    for ti in data.get("injuries", []) or []:
        side = "home" if ti.get("team", {}).get("homeAway") == "home" else ("away" if ti.get("team", {}).get("homeAway") == "away" else None)
        if side is None:
            # déterminer le camp via comparaison aux boxscore teams
            side = "home" if len(out["injuries"]["home"]) <= len(out["injuries"]["away"]) else "away"
        for p in ti.get("injuries", []) or []:
            a = p.get("athlete", {})
            out["injuries"][side].append({"name": a.get("displayName") or a.get("shortName"),
                                          "pos": (a.get("position") or {}).get("abbreviation", ""),
                                          "status": p.get("status", ""), "comment": p.get("shortComment", "")})
    # ---- Compositions ----
    rosters = data.get("rosters", []) or []
    if rosters:
        lu = []
        for rp in rosters[:2]:
            formation = rp.get("formation")
            players = []
            for entry in rp.get("roster", []) or []:
                a = entry.get("athlete", {})
                players.append({"name": a.get("displayName") or a.get("shortName"),
                                "pos": (a.get("position") or {}).get("abbreviation", ""),
                                "starter": bool(entry.get("starter")), "num": a.get("jersey") or ""})
            starters = [p for p in players if p["starter"]]
            subs = [p for p in players if not p["starter"]]
            lu.append({"name": rp.get("team", {}).get("displayName"), "home": rp.get("homeAway") == "home",
                       "formation": formation, "lineup": starters[:11], "subs": subs[:12],
                       "coach": None})
        out["lineups"] = lu
    # ---- Timeline (buts, cartons, remplacements) ----
    evts = data.get("keyEvents") or data.get("plays") or []
    for ev in evts:
        t = ev.get("type", {}) or {}
        tt = (t.get("text") or "").lower()
        icon = None
        if "penalty - scored" in tt or ("goal" in tt and "miss" not in tt and "own" not in tt): icon = "⚽"
        elif "own goal" in tt: icon = "⚽ (csc)"
        elif "yellow" in tt: icon = "🟨"
        elif "red" in tt or "second yellow" in tt: icon = "🟥"
        elif "substitution" in tt: icon = "🔄"
        elif "penalty - missed" in tt or "penalty - saved" in tt: icon = "❌ pen."
        if not icon: continue
        out["timeline"].append({"min": (ev.get("clock") or {}).get("displayValue") or (ev.get("time") or {}).get("displayValue") or "",
                                "icon": icon, "text": ev.get("text", ""), "team": (ev.get("team") or {}).get("displayName", "")})
    out["timeline"] = out["timeline"][-25:]
    # ---- Cotes ----
    for o in data.get("odds", []) or []:
        if not isinstance(o, dict): continue
        hml = (o.get("homeTeamOdds") or {}).get("moneyLine")
        aml = (o.get("awayTeamOdds") or {}).get("moneyLine")
        dml = (o.get("drawOdds") or {}).get("moneyLine")
        if hml is not None and aml is not None:
            ph, pa_ = american_to_prob(hml), american_to_prob(aml)
            pd_ = american_to_prob(dml) if dml is not None else max(0.05, 1 - (ph or 0) - (pa_ or 0))
            if ph and pa_:
                out["odds_probs"] = (ph, pd_ or 0.15, pa_)
                # Cotes décimales RÉELLES + bookmaker (§10 ODDS — auditabilité ROI)
                def _dec(ml):
                    try:
                        ml = float(ml)
                    except Exception:
                        return None
                    if ml == 0:
                        return None
                    return round(1 + (ml / 100.0 if ml > 0 else 100.0 / abs(ml)), 3)
                out["odds_provider"] = (o.get("provider") or {}).get("name")
                dec_d = _dec(dml) if dml is not None else (round(1.0 / pd_, 3) if pd_ else None)
                out["odds_dec"] = {"1": _dec(hml), "N": dec_d, "2": _dec(aml)}
            break
    return out

def fetch_summary(code, eid, ttl=20):
    key = f"sum:{code}:{eid}"
    cached = cache_get(key)
    if cached is not None: return cached
    data = http_json(ESPN_SUMMARY.format(code=code, eid=eid), retries=1)
    parsed = parse_summary(data)
    cache_set(key, parsed, ttl)
    return parsed

# ---------------------------------------------------------------------------
# Momentum + analyse d'expert (texte IA en français)
# ---------------------------------------------------------------------------
def compute_momentum(live_stats):
    """Retourne (dom_h, dom_a) entre 0 et 1 ; dom_h + dom_a = 1."""
    if not live_stats or len(live_stats) < 2: return None
    rows = {("h" if r["home"] else "a"): r for r in live_stats[:2]}
    h, a = rows.get("h", {}), rows.get("a", {})
    ph = h.get("poss", 50.0) / 100.0
    sh_h, sh_a = h.get("shots", 0), a.get("shots", 0)
    sot_h, sot_a = h.get("sot", 0), a.get("sot", 0)
    ck_h, ck_a = h.get("corners", 0), a.get("corners", 0)
    atk_h = sh_h * 0.8 + sot_h * 1.6 + ck_h * 0.4
    atk_a = sh_a * 0.8 + sot_a * 1.6 + ck_a * 0.4
    tot = atk_h + atk_a
    atk_dom_h = (atk_h / tot) if tot > 0.5 else 0.5
    dom_h = 0.55 * atk_dom_h + 0.45 * ph
    dom_h = min(max(dom_h, 0.05), 0.95)
    return dom_h, 1 - dom_h

FR_PICKS = {"1": "victoire à domicile", "N": "match nul", "2": "victoire à l'extérieur"}

def expert_analysis(home_name, away_name, pred, home_t, away_t, h2h, injuries, live_stats, league_name, state):
    """Génère l'analyse d'un expert en français, à partir des données réelles."""
    import random
    rnd = random.Random(home_name + away_name + pred["safePick"]["label"][:4])
    P = []

    # — Forme
    def form_str(t):
        if not t or not t["form"]: return None
        return "".join("V" if f["r"] == "W" else ("N" if f["r"] == "D" else "D") for f in t["form"][:5])
    fh, fa = form_str(home_t), form_str(away_t)
    if fh and fa:
        wh, wa = fh.count("V"), fa.count("V")
        if wh > wa + 1:
            P.append(f"{home_name} arrive avec une meilleure dynamique ({fh} sur ses 5 derniers matchs) que {away_name} ({fa}).")
        elif wa > wh + 1:
            P.append(f"{away_name} affiche une belle régularité ({fa}) quand {home_name} reste irrégulier ({fh}).")
        else:
            P.append(f"Les deux équipes présentent des dynamiques proches : {home_name} ({fh}) face à {away_name} ({fa}).")
    # — Forces dom/ext
    if home_t and home_t["sw"] > 0.1 and away_t and away_t["sw"] > 0.1:
        hgf = home_t["home_gf"] / home_t["home_sw"] if home_t["home_n"] else home_t["gf"] / home_t["sw"]
        agf = away_t["away_gf"] / away_t["away_sw"] if away_t["away_n"] else away_t["gf"] / away_t["sw"]
        aga = away_t["away_ga"] / away_t["away_sw"] if away_t["away_n"] else away_t["ga"] / away_t["sw"]
        hga = home_t["home_ga"] / home_t["home_sw"] if home_t["home_n"] else home_t["ga"] / home_t["sw"]
        P.append(f"{home_name} inscrit en moyenne {hgf:.1f} but et encaisse {hga:.1f} à domicile ; "
                 f"{away_name} marque {agf:.1f} but et concède {aga:.1f} à l'extérieur.")
    # — H2H
    if h2h:
        hw = sum(1 for m in h2h if (m["hg"] > m["ag"] and m["homeId"] == (home_t or {}).get("id")) or (m["ag"] > m["hg"] and m["awayId"] == (home_t or {}).get("id")))
        aw = sum(1 for m in h2h if (m["hg"] > m["ag"] and m["homeId"] == (away_t or {}).get("id")) or (m["ag"] > m["hg"] and m["awayId"] == (away_t or {}).get("id")))
        dn = len(h2h) - hw - aw
        if hw > aw: P.append(f"Historiquement, {home_name} a l'avantage dans les confrontations directes ({hw}V, {dn}N, {aw}D sur {len(h2h)} matchs récents).")
        elif aw > hw: P.append(f"Les confrontations directes récentes tournent plutôt en faveur de {away_name} ({aw}V, {dn}N, {hw}D).")
        else: P.append(f"Les {len(h2h)} derniers face-à-face sont parfaitement équilibrés ({hw}V - {dn}N - {aw}V).")
    # — Absences
    n_inj_h = len(injuries.get("home", [])); n_inj_a = len(injuries.get("away", []))
    if n_inj_h or n_inj_a:
        parts = []
        if n_inj_h: parts.append(f"{home_name} ({n_inj_h} absent{'s' if n_inj_h>1 else ''} signalé{'s' if n_inj_h>1 else ''})")
        if n_inj_a: parts.append(f"{away_name} ({n_inj_a} absent{'s' if n_inj_a>1 else ''} signalé{'s' if n_inj_a>1 else ''})")
        P.append("Point infirmerie : " + " · ".join(parts) + " — impact intégré dans le calcul du modèle.")
    # — Momentum live
    if live_stats and state == "in":
        mo = compute_momentum(live_stats)
        if mo:
            rows = {("h" if r["home"] else "a"): r for r in live_stats[:2]}
            h, a = rows.get("h", {}), rows.get("a", {})
            if mo[0] > 0.63:
                P.append(f"🔴 En direct : {home_name} domine nettement ({h.get('poss',50):.0f}% de possession, {int(h.get('shots',0))} tirs dont {int(h.get('sot',0))} cadrés, {int(h.get('corners',0))} corners).")
            elif mo[1] > 0.63:
                P.append(f"🔴 En direct : {away_name} prend l'ascendant ({a.get('poss',50):.0f}% de possession, {int(a.get('shots',0))} tirs dont {int(a.get('sot',0))} cadrés, {int(a.get('corners',0))} corners).")
            else:
                P.append(f"🔴 En direct : match équilibré ({h.get('poss',50):.0f}%-{a.get('poss',50):.0f}% de possession, {int(h.get('shots',0))} tirs contre {int(a.get('shots',0))}).")
    # — Conclusion
    sp = pred["safePick"]
    concl = rnd.choice([
        f"💎 Conclusion de l'IA : le pari le plus sûr est « {sp['label']} » ({sp['p']}%).",
        f"💎 Verdict : « {sp['label']} » se détache comme l'option la plus solide ({sp['p']}%).",
        f"💎 Synthèse : le modèle désigne « {sp['label']} » comme prono le plus fiable ({sp['p']}%).",
    ])
    stars = "★" * sp["stars"] + "☆" * (5 - sp["stars"])
    P.append(concl + f" Confiance : {stars}.")
    return P

# ---------------------------------------------------------------------------
# PERSISTANCE + SOCLE DE VÉRITÉ (ÉTAPE 2A)
# ---------------------------------------------------------------------------
# L'ancien système « accuracy » (accuracy.json) a été SUPPRIMÉ : il recalculait
# la prédiction APRÈS le match avec des données incluant le match lui-même, puis
# la notait comme si elle avait été faite avant — métrique invalide (fuite de
# données, audit ÉTAPE 1 §6). Aucune donnée historique n'est importée ni
# falsifiée (§21) : le fichier accuracy.json éventuel est simplement ignoré.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

BUILD_MARKER = "3.0-socle"
_MATCH_MEM = {}   # mid -> empreinte du dernier état persisté (évite les écritures inutiles)

def boot_data_layer():
    """§18 — DÉMARRAGE : DATABASE → vérification schéma/migrations → restauration
    de la sauvegarde si disque neuf → modèle actif → cache → workers → Live."""
    backup.restore_if_needed()          # disque éphémère de l'hébergement gratuit
    db_layer.init()                     # migrations idempotentes (§19)
    repo.register_model(MODEL_NAME, MODEL_VERSION, MODEL_CONFIG)
    # §17 : la RAM est un CACHE — au redémarrage on restaure les caches lourds
    # persistés (stats d'équipes ~150 j, classements) au lieu de tout reconstruire.
    restored = 0
    for key, (expires_at, payload) in repo.cache_load_all().items():
        with _cache_lock:
            _cache[key] = (expires_at, payload)
        restored += 1
    c = db_layer.table_counts()
    print(f"[boot] bd prête (migration v{db_layer.migration_version()}) — "
          f"matchs={c['matches']} predictions={c['predictions']} resultats={c['results']} "
          f"snapshots={c['data_snapshots']} · caches restaurés={restored}")

def persist_cache(key, value, ttl):
    """Persiste les caches LOURDS (stats d'équipes, classements → §18)."""
    try:
        repo.cache_save(key, json.dumps(value, ensure_ascii=False), ttl)
    except Exception:
        pass

def espn_state_to_status(ev):
    return {"pre": "UPCOMING", "in": "LIVE", "post": "FINISHED"}.get(ev.get("state"), "UPCOMING")

def persist_match(code, ev):
    """§4 — le match entre dans la persistance (IDENTITÉ STABLE source + id).
    N'écrit que si l'état a réellement changé (horloges exclues — anti-spam)."""
    mid = f"espn:{code}:{ev['id']}"
    hsh = hash((ev["state"], ev["home"]["score"], ev["away"]["score"], ev["utc"], ev.get("venue")))
    if _MATCH_MEM.get(mid) == hsh:
        return mid
    try:
        repo.upsert_match({
            "id": mid, "source": "espn", "source_match_id": ev["id"],
            "fallback_key": repo.fallback_key(code, ev["utc"], ev["home"]["full"] or ev["home"]["name"],
                                              ev["away"]["full"] or ev["away"]["name"]),
            "competition": code,
            "home_team": ev["home"]["full"] or ev["home"]["name"],
            "away_team": ev["away"]["full"] or ev["away"]["name"],
            "home_team_ext_id": str(ev["home"]["id"]), "away_team_ext_id": str(ev["away"]["id"]),
            "kickoff_time_utc": ev["utc"], "status": espn_state_to_status(ev),
            "home_score": None if ev["home"]["score"] is None else to_int(ev["home"]["score"]),
            "away_score": None if ev["away"]["score"] is None else to_int(ev["away"]["score"]),
            "home_ht_score": ev["home"].get("ht"), "away_ht_score": ev["away"].get("ht"),
            "venue": ev.get("venue", "")})
        _MATCH_MEM[mid] = hsh
    except Exception as ex:
        print(f"[persist] {mid}: {type(ex).__name__}: {ex}")
    return mid


# ---------------------------------------------------------------------------
# ÉTAT GLOBAL + BOUCLES TEMPS RÉEL + SSE
# ---------------------------------------------------------------------------
STATE = {"feed": None, "version": 0, "liveCount": 0, "topPicks": [], "updatedAt": None}
STATE_LOCK = threading.Lock()

SSE_CLIENTS = []
SSE_LOCK = threading.Lock()

def sse_broadcast(payload_str):
    with SSE_LOCK:
        dead = []
        for q in SSE_CLIENTS:
            try: q.put_nowait(payload_str)
            except Exception: dead.append(q)
        for q in dead:
            try: SSE_CLIENTS.remove(q)
            except ValueError: pass

def compute_top_picks(feed_blocks, limit=14):
    """Pronos d'Or : UNIQUEMENT des prédictions réellement GELÉES sur des matchs
    à venir (§27). Plus jamais de « prono » recalculé sur match live/terminé."""
    cands = []
    for day in feed_blocks:
        for lg in day["leagues"]:
            for m in lg["matches"]:
                p = m.get("pred")
                if not p or m["state"] != "pre": continue
                if not p.get("frozen"): continue
                sp = p["safePick"]
                if sp["p"] < 58: continue
                cands.append({"code": lg["code"], "lname": lg["sname"], "flag": lg["flag"],
                              "id": m["id"], "day": m["day"], "state": m["state"],
                              "home": m["home"]["name"], "away": m["away"]["name"],
                              "utc": m["utc"], "clock": m.get("clock", ""),
                              "score": None,
                              "label": sp["label"], "p": sp["p"], "stars": sp["stars"], "kind": sp["kind"],
                              "reliab": p["reliab"], "frozenAt": p.get("frozenAt"), "v": p.get("v")})
    cands.sort(key=lambda c: (c["p"] * (0.8 + 0.2 * c["reliab"])), reverse=True)
    # éviter doublons par match
    seen, out = set(), []
    for c in cands:
        if c["id"] in seen: continue
        seen.add(c["id"]); out.append(c)
        if len(out) >= limit: break
    return out

def enrich_match(ev, code):
    """Attache au match la donnée de prédiction HONNÊTE selon son état (§10/§11) :

    À VENIR   → version gelée du moment (publiée immédiatement, immuable) ;
    EN DIRECT → la prédiction GELÉE pré-match relue depuis la persistance
                (JAMAIS recalculée avec le score/stats du match) ;
    TERMINÉ   → la prédiction GELÉE pré-match + son verdict réel, ou
                « non évalué » si aucune n'a été gelée avant le coup d'envoi.
    """
    m = dict(ev)
    stats = cache_get(f"stats:{code}")
    m["pred"] = None
    m["predPending"] = False
    m["notEvaluable"] = False
    persist_match(code, ev)   # identité stable en base (§4) — écrit seulement si changement
    if ev["state"] == "pre":
        m["predPending"] = stats is None
        if stats is not None:
            try:
                det = cache_get(f"predetail:{code}:{ev['id']}")
                m["pred"] = predsvc.publish_match(code, ev, stats, det)
                predsvc.ensure_t15_reference(code, ev)
            except Exception as ex:
                print(f"[pred] {code}/{ev['id']}: {type(ex).__name__}: {ex}")
    elif ev["state"] == "in":
        liveX = cache_get(f"livedetail:{code}:{ev['id']}")
        if liveX:
            m["liveStats"] = liveX.get("liveStats")
            m["tl_count"] = len(liveX.get("timeline", []))
        try:
            m["pred"] = predsvc.frozen_display(code, ev)
        except Exception as ex:
            print(f"[pred] {code}/{ev['id']}: {type(ex).__name__}: {ex}")
    elif ev["state"] == "post":
        try:
            predsvc.settle_if_finished(code, ev)   # règlement unique (anti-fabrication §11)
            disp = predsvc.frozen_display(code, ev, with_verdicts=True)
            m["pred"] = disp
            m["notEvaluable"] = disp is None
        except Exception as ex:
            print(f"[settle] {code}/{ev['id']}: {type(ex).__name__}: {ex}")
    return m

def build_feed_payload():
    today = datetime.now(timezone.utc).date()
    chunks = date_chunks(today - timedelta(days=FEED_PAST), today + timedelta(days=FEED_AHEAD), 9)
    def league_block(lg):
        code = lg["code"]
        try: data = fetch_league_window(code, chunks, ttl=45)
        except Exception as ex:
            print(f"[feed] {code}: {ex}"); return None
        if not data["events"]: return None
        ensure_stats_async(code)
        matches = [enrich_match(ev, code) for ev in data["events"]]
        return {"code": code, "name": lg["name"], "sname": lg["sname"], "flag": lg["flag"],
                "group": lg["group"], "logo": data.get("leagueLogo"), "matches": matches}
    with ThreadPoolExecutor(max_workers=14) as ex:
        blocks = [b for b in ex.map(league_block, LEAGUES) if b]
    days, live_count, live_ids = {}, 0, []
    for b in blocks:
        for m in b["matches"]:
            if m["state"] == "in":
                live_count += 1; live_ids.append((b["code"], m["id"]))
            days.setdefault(m["day"], []).append((b, m))
    feed_blocks = []
    for day in sorted(days):
        by_league = {}
        for b, m in days[day]:
            by_league.setdefault(b["code"], {"code": b["code"], "name": b["name"], "sname": b["sname"],
                                             "flag": b["flag"], "group": b["group"], "logo": b.get("logo"),
                                             "matches": []})["matches"].append(m)
        feed_blocks.append({"day": day, "leagues": sorted(by_league.values(), key=lambda x: (x["group"], x["name"]))})
    top = compute_top_picks(feed_blocks)
    payload = {"generatedAt": datetime.now(timezone.utc).isoformat(), "liveCount": live_count,
               "statsReady": sum(1 for lg in LEAGUES if cache_get(f"stats:{lg['code']}") is not None),
               "statsTotal": len(LEAGUES), "topPicks": top, "evaluation": predsvc.public_performance(),
               "days": feed_blocks,
               "liveIds": ["%s:%s" % x for x in live_ids]}
    return payload

def feed_signature(payload):
    """Empreinte des données qui comptent (hors horloges) : on ne pousse vers
    les navigateurs que si quelque chose a VRAIMENT changé."""
    parts = []
    for day in payload["days"]:
        for lg in day["leagues"]:
            for m in lg["matches"]:
                p = m.get("pred") or {}
                st = ""
                if m.get("liveStats"):
                    st = "|".join(f"{round(r.get('poss', 0))}{round(r.get('shots', 0))}{round(r.get('corners', 0))}" for r in m["liveStats"])
                parts.append(f"{m['id']}{m['state']}{m['home']['score']}{m['away']['score']}{p.get('p1')}{p.get('p2')}{st}")
    return hash(tuple(parts))

BCAST_COUNTER = {"n": 0}

def refresh_state(force=False):
    payload = build_feed_payload()
    sig = feed_signature(payload)
    with STATE_LOCK:
        changed = sig != STATE.get("sig")
        if changed:
            STATE["version"] += 1
        STATE["sig"] = sig
        STATE["feed"] = payload
        STATE["liveCount"] = payload["liveCount"]
        STATE["updatedAt"] = payload["generatedAt"]
        v = STATE["version"]
    BCAST_COUNTER["n"] += 1
    # Push si changement réel, ou toutes les ~4 boucles pour rafraîchir l'horloge de base
    if changed or BCAST_COUNTER["n"] % 4 == 0:
        sse_broadcast(json.dumps({"type": "feed", "v": v, "data": payload}, ensure_ascii=False, separators=(",", ":")))
    return payload

def get_live_match_ids():
    with STATE_LOCK:
        f = STATE["feed"]
    if not f: return []
    return [x.split(":") for x in f.get("liveIds", [])]

def feed_loop():
    """Boucle principale : met à jour le flux en continu et le pousse."""
    time.sleep(0.5)
    while True:
        try:
            payload = refresh_state()
            wait = LIVE_INTERVAL if payload["liveCount"] > 0 else FEED_INTERVAL_IDLE
        except Exception as e:
            print("[feed_loop]", e)
            wait = 30
        time.sleep(wait)

def live_detail_loop():
    """Boucle live : récupère stats/cotes/blessures des matchs en cours."""
    time.sleep(4)
    while True:
        ids = get_live_match_ids()
        if not ids:
            time.sleep(25); continue

def get_prematch_ids(minutes=90):
    """Matchs À VENIR dont le coup d'envoi est dans moins de `minutes` min."""
    with STATE_LOCK:
        f = STATE["feed"]
    if not f:
        return []
    now = datetime.now(timezone.utc)
    out = []
    for day in f.get("days", []):
        for lg in day["leagues"]:
            for m in lg["matches"]:
                if m["state"] != "pre":
                    continue
                try:
                    ko = datetime.fromisoformat(m["utc"].replace("Z", "+00:00"))
                except Exception:
                    continue
                delta = (ko - now).total_seconds()
                if -900 < delta <= minutes * 60:
                    out.append((lg["code"], m["id"]))
    return out[:24]

def prematch_detail_loop():
    """Récupère compositions/blessures/cotes des matchs proches du coup d'envoi.
    Une composition officielle publiée après une première version déclenche
    automatiquement une NOUVELLE VERSION gelée (§8) — l'ancienne reste intacte."""
    time.sleep(7)
    while True:
        try:
            for code, eid in get_prematch_ids(90):
                try:
                    parsed = fetch_summary(code, eid, ttl=240)
                    cache_set(f"predetail:{code}:{eid}", parsed, 600)
                except Exception as e:
                    print(f"[pre_detail] {code}/{eid}: {e}")
        except Exception as e:
            print("[pre_detail]", e)
        time.sleep(180)
        for code, eid in ids:
            try:
                parsed = fetch_summary(code, eid, ttl=LIVE_INTERVAL - 2)
                cache_set(f"livedetail:{code}:{eid}", parsed, 120)
            except Exception as e:
                print(f"[live_detail] {code}/{eid}: {e}")
        time.sleep(max(8, LIVE_INTERVAL - 6))

# ---------------------------------------------------------------------------
# Routes Flask
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", static_url_path="/static")

@app.route("/")
def index(): return send_from_directory("static", "index.html")

@app.route("/healthz")
def healthz():
    with STATE_LOCK:
        v, lc, up = STATE["version"], STATE["liveCount"], STATE["updatedAt"]
    dbc = db_layer.table_counts()
    return {"ok": True, "version": v, "live": lc, "updatedAt": up, "build": BUILD_MARKER,
            "sseClients": len(SSE_CLIENTS),
            "statsReady": sum(1 for lg in LEAGUES if cache_get(f"stats:{lg['code']}") is not None),
            "db": {"migration": db_layer.migration_version(), "integrity": bool(db_layer.integrity_check()),
                   "matches": dbc["matches"], "predictions": dbc["predictions"],
                   "snapshots": dbc["data_snapshots"], "results": dbc["results"]}}

@app.route("/api/config")
def api_config():
    return jsonify({"leagues": LEAGUES, "groups": GROUPS,
                    "serverTime": datetime.now(timezone.utc).isoformat()})

def feed_response():
    with STATE_LOCK:
        payload = STATE["feed"]
    if payload is None:
        payload = refresh_state()
    body = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    resp = Response(body, content_type="application/json; charset=utf-8")
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/feed")
def api_feed():
    if request.args.get("force") == "1":
        refresh_state(force=True)
    return feed_response()

@app.route("/api/stream")
def api_stream():
    """Flux SSE temps réel consommé par le frontend."""
    q = queue.Queue(maxsize=40)
    with SSE_LOCK:
        SSE_CLIENTS.append(q)
    def gen():
        try:
            yield "retry: 3000\n\n"
            with STATE_LOCK:
                f, v = STATE["feed"], STATE["version"]
            if f:
                yield "data: " + json.dumps({"type": "feed", "v": v, "data": f}, ensure_ascii=False, separators=(",", ":")) + "\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield "data: " + msg + "\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with SSE_LOCK:
                try: SSE_CLIENTS.remove(q)
                except ValueError: pass
    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp

@app.route("/api/standings/<code>")
def api_standings(code):
    skey = f"standings:{code}"
    cached = cache_get(skey)
    if cached is None:
        try:
            d = http_json(ESPN_STANDINGS.format(code=code))
            rows = []
            for child in d.get("children", []):
                for e in (child.get("standings") or {}).get("entries", []):
                    st = {s["name"]: s.get("displayValue") for s in e.get("stats", []) if "name" in s}
                    team = e.get("team", {})
                    rows.append({"rank": st.get("rank", "?"), "name": team.get("displayName") or team.get("name"),
                                 "logo": (team.get("logos") or [{}])[0].get("href") if team.get("logos") else None,
                                 "j": st.get("gamesPlayed", "-"), "v": st.get("wins", "-"), "n": st.get("ties", "-"),
                                 "d": st.get("losses", "-"), "bp": st.get("pointsFor", "-"), "bc": st.get("pointsAgainst", "-"),
                                 "diff": st.get("pointDifferential", "-"), "pts": st.get("points", "-")})
            def toint(x):
                try: return int(x)
                except Exception: return 999
            rows.sort(key=lambda r: toint(r["rank"]))
            cached = {"ok": True, "rows": rows}
        except Exception as ex:
            cached = {"ok": False, "error": str(ex), "rows": []}
        cache_set(skey, cached, 30 * 60)
        persist_cache(skey, cached, 30 * 60)
    return jsonify(cached)

@app.route("/api/match/<code>/<event_id>")
def api_match_detail(code, event_id):
    """Analyse complète : stats live, timeline, compos, blessés, H2H, texte IA."""
    today = datetime.now(timezone.utc).date()
    chunks = date_chunks(today - timedelta(days=FEED_PAST), today + timedelta(days=FEED_AHEAD), 9)
    data = fetch_league_window(code, chunks, ttl=90)
    target = next((ev for ev in data["events"] if ev["id"] == event_id), None)
    if target is None:
        return jsonify({"ok": False, "error": "Match introuvable"}), 404

    # Résumé ESPN détaillé (live ou pré-match) — récupération directe
    try:
        summ = fetch_summary(code, event_id, ttl=15 if target["state"] == "in" else 120)
    except Exception:
        summ = {"liveStats": None, "timeline": [], "injuries": {"home": [], "away": []},
                "lineups": None, "odds_probs": None, "seasonStats": None,
                "odds_provider": None, "odds_dec": None}

    stats = cache_get(f"stats:{code}")
    if stats is None:
        ensure_stats_async(code)
        return jsonify({"ok": True, "pending": True, "match": target, "home": None, "away": None, "h2h": []})

    home = stats["teams"].get(target["home"]["id"])
    away = stats["teams"].get(target["away"]["id"])
    h2h = []
    if home:
        for m in home["matches"]:
            if m["homeId"] == target["away"]["id"] or m["awayId"] == target["away"]["id"]:
                h2h.append(m)
        h2h = h2h[:6]

    def team_info(t):
        if not t: return None
        return {"name": t["name"], "logo": t["logo"], "n": t["n"], "w": t["w"], "d": t["d"], "l": t["l"],
                "gf": round(t["gf"] / max(t["sw"], 0.01), 2), "ga": round(t["ga"] / max(t["sw"], 0.01), 2),
                "form": t["form"]}

    # Prédiction HONNÊTE (§9/§10/§11) :
    # - À VENIR  → gel immédiat de la version du moment (compos officielles →
    #   nouvelle version automatique, anciennes conservées — §8) ;
    # - EN DIRECT / TERMINÉ → relecture de la prédiction GELÉE pré-match telle
    #   qu'archivée. Interdiction absolue de recalculer avec les données du match
    #   ou d'après-match : si rien n'a été gelé avant le coup d'envoi, le match
    #   est « Not évalué », jamais « prédit après coup ».
    persist_match(code, target)
    pred = None
    if home is not None:
        try:
            if target["state"] == "pre":
                pred = predsvc.publish_match(code, target, stats, summ)
            elif target["state"] == "in":
                pred = predsvc.frozen_display(code, target)
            else:
                predsvc.settle_if_finished(code, target)
                pred = predsvc.frozen_display(code, target, with_verdicts=True)
        except Exception as ex:
            print(f"[pred/detail] {code}/{event_id}: {type(ex).__name__}: {ex}")

    analysis = []
    if home is not None and pred is not None:
        analysis = expert_analysis(target["home"]["full"] or target["home"]["name"],
                                   target["away"]["full"] or target["away"]["name"],
                                   pred, home, away, h2h, summ["injuries"], summ.get("liveStats"),
                                   LEAGUE_BY_CODE.get(code, {}).get("name", code), target["state"])

    return jsonify({"ok": True, "match": target, "pred": pred,
                    "home": team_info(home), "away": team_info(away), "h2h": h2h,
                    "analysis": analysis,
                    "liveStats": summ.get("liveStats"), "timeline": summ.get("timeline", []),
                    "injuries": summ["injuries"], "lineups": summ.get("lineups"),
                    "seasonStats": summ.get("seasonStats")})

# ---------------------------------------------------------------------------
# API HISTORIQUE & ÉVALUATION (§25) — alimentée EXCLUSIVEMENT par la base
# persistante (prédictions gelées + règlements). Jamais de recalcul.
# ---------------------------------------------------------------------------
@app.route("/api/predictions/history")
def api_pred_history():
    """Historique immuable des prédictions gelées (§14) : une ligne par famille
    de marché et par version de référence, avec verdict si réglée."""
    limit = min(200, max(1, to_int(request.args.get("limit"), 50)))
    offset = max(0, to_int(request.args.get("offset"), 0))
    try:
        return jsonify(predsvc.history(
            limit=limit, offset=offset, market=request.args.get("market"),
            competition=request.args.get("competition"), status=request.args.get("status")))
    except Exception as ex:
        return jsonify({"ok": False, "error": f"{type(ex).__name__}: {ex}"}), 500


@app.route("/api/predictions/performance")
def api_pred_performance():
    """Métriques propres §12 (accuracy, Brier, LogLoss, ROI/Yield, drawdown,
    par marché/compétition/version) — ou statut « rebuilding » tant que
    l'échantillon de prédictions gelées réglées est insuffisant."""
    try:
        return jsonify(predsvc.public_performance(force=request.args.get("force") == "1"))
    except Exception as ex:
        return jsonify({"ok": False, "error": f"{type(ex).__name__}: {ex}"}), 500


@app.route("/api/predictions/calibration")
def api_pred_calibration():
    """Calibration §13 : probabilités publiées vs fréquences observées."""
    try:
        return jsonify(predsvc.calibration_bins())
    except Exception as ex:
        return jsonify({"ok": False, "error": f"{type(ex).__name__}: {ex}"}), 500


@app.route("/api/predictions/<pid>")
def api_pred_detail(pid):
    """Détail d'UNE prédiction gelée : valeurs, snapshot complet (« ce que le
    modèle savait exactement à cet instant » — §5), hash de vérification (§15)."""
    try:
        d = predsvc.prediction_detail(pid)
        if not d:
            return jsonify({"ok": False, "error": "Prédiction introuvable"}), 404
        return jsonify({"ok": True, **d})
    except Exception as ex:
        return jsonify({"ok": False, "error": f"{type(ex).__name__}: {ex}"}), 500


@app.route("/api/matches/<path:mid>/predictions")
def api_match_predictions(mid):
    """Toutes les versions gelées d'un match + journal d'audit + résultat."""
    try:
        mid = mid if mid.startswith("espn:") else f"espn:x:{mid}"
        d = predsvc.match_predictions(mid)
        if not d:
            return jsonify({"ok": False, "error": "Match introuvable"}), 404
        return jsonify({"ok": True, **d})
    except Exception as ex:
        return jsonify({"ok": False, "error": f"{type(ex).__name__}: {ex}"}), 500


# ---------------------------------------------------------------------------
# Démarrage
# ---------------------------------------------------------------------------
def warm_stats():
    """Pré-chauffe UNIQUEMENT les ligues qui ont des matchs dans la fenêtre
    (allège énormément le CPU sur l'offre gratuite au démarrage)."""
    time.sleep(2)
    today = datetime.now(timezone.utc).date()
    chunks = date_chunks(today - timedelta(days=FEED_PAST), today + timedelta(days=FEED_AHEAD), 9)
    def has_events(lg):
        try:
            return bool(fetch_league_window(lg["code"], chunks, ttl=90)["events"])
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=12) as ex:
        actives = [lg for lg, ok in zip(LEAGUES, ex.map(has_events, LEAGUES)) if ok]
    print(f"[warm] {len(actives)} ligues avec matchs à analyser")
    def st(lg):
        try: build_stats(lg["code"])
        except Exception as e: print(f"[warm] {lg['code']}: {e}")
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(st, actives))
    print("[warm] stats prêtes")
    try: refresh_state()
    except Exception: pass

def keep_awake_loop():
    """Anti-sommeil : l'app se ping elle-même via son URL publique toutes les 5 min,
    ce qui génère du trafic entrant et empêche la mise en veille (Render free, etc.).
    Actif uniquement si l'hébergeur fournit le nom d'hôte public (RENDER_EXTERNAL_HOSTNAME)
    ou si SELF_URL est défini."""
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or os.environ.get("SELF_URL")
    if not host:
        return
    url = host if host.startswith("http") else f"https://{host}"
    print(f"[keepawake] actif — auto-ping {url}/healthz toutes les 300s")
    time.sleep(60)  # laisser l'app démarrer complètement
    while True:
        try:
            http_json(url + "/healthz", retries=0, timeout=12)
        except Exception as e:
            print(f"[keepawake] ping raté: {e}")
        time.sleep(300)

def start_workers():
    """§18 — SÉQUENCE DE DÉMARRAGE : DATABASE → schéma/migrations → restauration
    → modèle actif → cache → workers → Live → SSE. Aucune reconstruction longue :
    les caches lourds (stats 150 j, classements) sont restaurés de la persistance."""
    boot_data_layer()
    threading.Thread(target=feed_loop, daemon=True).start()
    threading.Thread(target=live_detail_loop, daemon=True).start()
    threading.Thread(target=prematch_detail_loop, daemon=True).start()
    threading.Thread(target=warm_stats, daemon=True).start()
    threading.Thread(target=keep_awake_loop, daemon=True).start()
    backup.start()


if os.environ.get("PRONOFOOT_NO_THREADS") != "1":
    start_workers()
else:
    # Mode test / import : base initialisable à la demande, workers désactivés.
    db_layer.init()
    repo.register_model(MODEL_NAME, MODEL_VERSION, MODEL_CONFIG)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    try:
        # Serveur de production (Waitress) — supporte le streaming SSE
        from waitress import serve
        print(f"[prod] Waitress sur 0.0.0.0:{port}")
        serve(app, host="0.0.0.0", port=port, threads=24)
    except ImportError:
        print(f"[dev] Flask sur 0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
