# -*- coding: utf-8 -*-
"""
⚽ PronoFoot Live 2.0 — Application de pronostics football TEMPS RÉEL
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

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={rng}"
ESPN_STANDINGS  = "https://site.web.api.espn.com/apis/v2/sports/soccer/{code}/standings"
ESPN_SUMMARY    = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/summary?event={eid}"

STATS_DAYS = 150
FEED_PAST, FEED_AHEAD = 1, 8
LIVE_INTERVAL = 15      # secondes entre deux boucles live
FEED_INTERVAL_IDLE = 90

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
def parse_event(e, lmeta):
    try:
        comp = e["competitions"][0]
        st = e["status"]["type"]
        home = away = None
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            node = {"id": t.get("id"), "name": t.get("shortDisplayName") or t.get("displayName") or t.get("name"),
                    "full": t.get("displayName") or t.get("name"), "logo": t.get("logo"),
                    "score": c.get("score"), "winner": c.get("winner", False), "shootout": c.get("shootoutScore")}
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
                if logos: league_logo = logos[0].get("href")
            for e in d.get("events", []):
                p = parse_event(e, lmeta)
                if p: events[p["id"]] = p
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
# MOTEUR IA — Poisson + momentum + cotes + pari le plus sûr
# ---------------------------------------------------------------------------
def poisson(k, lam): return math.exp(-lam) * lam ** k / math.factorial(k)

def team_rates(t, loc):
    if t["sw"] <= 0.05: return None, None
    gf_overall, ga_overall = t["gf"] / t["sw"], t["ga"] / t["sw"]
    if t[f"{loc}_n"] >= 3 and t[f"{loc}_sw"] > 0.3:
        return 0.65 * (t[f"{loc}_gf"] / t[f"{loc}_sw"]) + 0.35 * gf_overall, \
               0.65 * (t[f"{loc}_ga"] / t[f"{loc}_sw"]) + 0.35 * ga_overall
    return gf_overall, ga_overall

def shrink(avg, league_avg, sw, k=3.0):
    return (avg * sw + league_avg * k) / (sw + k)

def american_to_prob(ml):
    try: ml = float(ml)
    except Exception: return None
    if ml == 0: return None
    if ml > 0: return 100.0 / (ml + 100.0)
    return -ml / (-ml + 100.0)

def build_markets(p1, px, p2, over15, over25, over35, btts):
    u25, u35 = 1 - over25, 1 - over35
    return [
        ("Victoire " + "domicile (1)", p1, "1N2"), ("Match nul (N)", px, "1N2"), ("Victoire extérieur (2)", p2, "1N2"),
        ("1N — Dom. ou Nul", p1 + px, "DC"), ("N2 — Nul ou Ext.", px + p2, "DC"), ("12 — Pas de nul", p1 + p2, "DC"),
        ("Plus de 1,5 but", over15, "BUTS"), ("Plus de 2,5 buts", over25, "BUTS"),
        ("Moins de 2,5 buts", u25, "BUTS"), ("Moins de 3,5 buts", u35, "BUTS"),
        ("Les 2 équipes marquent : OUI", btts, "BTTS"), ("Les 2 équipes marquent : NON", 1 - btts, "BTTS"),
    ]

def predict_match(stats, home_id, away_id, minute=None, score=None, momentum=None, odds=None, absences=(0, 0)):
    """Retourne probabilités complètes + marchés + pari le plus sûr."""
    lg = stats["league"]
    home, away = stats["teams"].get(home_id), stats["teams"].get(away_id)
    if home is None or away is None: return None

    h_att, h_def = team_rates(home, "home"); a_att, a_def = team_rates(away, "away")
    reliab = min(1.0, (home["sw"] + away["sw"]) / 14.0)
    if h_att is None: h_att = lg["homeAvg"]
    if a_att is None: a_att = lg["awayAvg"]
    if h_def is None: h_def = lg["homeAvg"]
    if a_def is None: a_def = lg["awayAvg"]

    h_att = shrink(h_att, lg["homeAvg"], home["sw"]); a_def = shrink(a_def, lg["awayAvg"], away["sw"])
    a_att = shrink(a_att, lg["awayAvg"], away["sw"]); h_def = shrink(h_def, lg["homeAvg"], home["sw"])

    lam_h = min(max(h_att * a_def / max(lg["awayAvg"], 0.5) * lg["homeAvg"] / max(lg["homeAvg"], 0.5), 0.15), 4.5)
    lam_a = min(max(a_att * h_def / max(lg["homeAvg"], 0.5) * lg["awayAvg"] / max(lg["awayAvg"], 0.5), 0.10), 4.0)

    # Ajustement absences (blessés/suspendus majeurs)
    lam_h *= 1 - min(4, absences[0]) * 0.025
    lam_a *= 1 - min(4, absences[1]) * 0.025

    live = minute is not None and score is not None
    cur_h = cur_a = 0
    if live:
        cur_h, cur_a = score
        m = min(max(minute or 1, 1), 90)
        remaining = (90 - m) / 90.0
        lam_h = (1e-6, 1e-6)[0] if remaining <= 0.01 else lam_h * remaining
        lam_a = 1e-6 if remaining <= 0.01 else lam_a * remaining
        # Momentum live : domination mesurée sur les stats du match
        if momentum:
            dom_h, dom_a = momentum  # 0..1 chacun (somme ~1)
            lam_h *= min(max(0.35 + 1.3 * dom_h, 0.5), 1.75)
            lam_a *= min(max(0.35 + 1.3 * dom_a, 0.5), 1.75)

    MAXG = 10
    p1 = px = p2 = over15 = over25 = over35 = btts = 0.0
    cells = []
    for i in range(MAXG + 1):
        pi = poisson(i, lam_h)
        for j in range(MAXG + 1):
            p = pi * poisson(j, lam_a)
            fh, fa = cur_h + i, cur_a + j
            if fh > fa: p1 += p
            elif fh == fa: px += p
            else: p2 += p
            tg = i + j
            if tg > 1.5: over15 += p
            if tg > 2.5: over25 += p
            if tg > 3.5: over35 += p
            if (cur_h > 0 or i > 0) and (cur_a > 0 or j > 0): btts += p
            cells.append((p, fh, fa))
    cells.sort(reverse=True)

    # Fusion adaptative des cotes bookmaker : plus le modèle manque de données,
    # plus on fait confiance au marché (25 % → jusqu'à 65 %)
    odds_info = None
    if odds:
        ph, pd_, pa_ = odds  # bruts (avec marge)
        tot = ph + pd_ + pa_
        if tot > 0.5:
            mh, md, ma = ph / tot, pd_ / tot, pa_ / tot
            w = min(0.65, 0.25 + 0.45 * (1 - reliab))
            p1, px, p2 = (1 - w) * p1 + w * mh, (1 - w) * px + w * md, (1 - w) * p2 + w * ma
            odds_info = {"p1": round(mh * 100), "px": round(md * 100), "p2": round(ma * 100), "w": round(w * 100)}

    markets = build_markets(p1, px, p2, over15, over25, over35, btts)
    # Pari le plus sûr : probabilité × fiabilité
    safe = None
    for label, prob, kind in markets:
        score_conf = prob * (0.75 + 0.25 * reliab)
        if safe is None or score_conf > safe[1] * (0.75 + 0.25 * reliab) * 1.0:
            if safe is None or prob * (0.75 + 0.25 * reliab) > safe[1]:
                safe = (label, prob * (0.75 + 0.25 * reliab), prob, kind)
    maxp = max(p1, px, p2)
    conf = (maxp - 1 / 3) / (2 / 3) * (0.35 + 0.65 * reliab)
    stars = max(1, min(5, round(1 + 4 * conf)))
    safe_prob = safe[2]
    safe_stars = 1 if safe_prob < 0.55 else (2 if safe_prob < 0.65 else (3 if safe_prob < 0.72 else (4 if safe_prob < 0.82 else 5)))

    return {"p1": round(p1 * 100, 1), "px": round(px * 100, 1), "p2": round(p2 * 100, 1),
            "pick": "1" if p1 >= px and p1 >= p2 else ("N" if px >= p2 else "2"),
            "topScores": [{"s": f"{fh}-{fa}", "p": round(p * 100, 1)} for p, fh, fa in cells[:5]],
            "over15": round(over15 * 100, 1), "over25": round(over25 * 100, 1), "over35": round(over35 * 100, 1),
            "under25": round((1 - over25) * 100, 1), "under35": round((1 - over35) * 100, 1),
            "btts": round(btts * 100, 1),
            "markets": [{"label": l, "p": round(p * 100, 1), "kind": k} for l, p, k in markets],
            "safePick": {"label": safe[0], "p": round(safe[2] * 100, 1), "stars": safe_stars, "kind": safe[3]},
            "xgH": round(lam_h, 2), "xgA": round(lam_a, 2),
            "stars": stars, "reliab": round(reliab, 2), "live": live,
            "marketOdds": odds_info}

# ---------------------------------------------------------------------------
# PARSING SUMMARY (stats live, timeline, compos, blessés, cotes)
# ---------------------------------------------------------------------------
STAT_LABELS = {"Possession": "poss", "Shots": "shots", "On Goal": "sot", "Corner Kicks": "corners",
               "Fouls": "fouls", "Yellow Cards": "yellow", "Red Cards": "red", "Saves": "saves",
               "Offsides": "offsides"}

def parse_summary(data):
    """Extrait tout le nécessaire d'un résumé ESPN (live, pre ou post)."""
    out = {"liveStats": None, "timeline": [], "injuries": {"home": [], "away": []},
           "lineups": None, "odds_probs": None, "seasonStats": None}
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

def market_decided(label, hg, ag):
    """Un marché live est-il déjà tranché (gagné ou perdu d'avance) ?"""
    tot = hg + ag
    if "marquent : OUI" in label: return hg > 0 and ag > 0
    if "marquent : NON" in label: return False
    if label.startswith("Plus de"):
        thr = to_float(label.split("Plus de ")[1].split(" but")[0].replace(",", "."), 99)
        return tot > thr + 0.5
    if label.startswith("Moins de"):
        thr = to_float(label.split("Moins de ")[1].split(" but")[0].replace(",", "."), 99)
        return tot > thr
    return False

def compute_top_picks(feed_blocks, limit=14):
    """Classe les pronos les plus sûrs du moment (live + à venir)."""
    cands = []
    for day in feed_blocks:
        for lg in day["leagues"]:
            for m in lg["matches"]:
                p = m.get("pred")
                if not p or m["state"] == "post": continue
                sp = p["safePick"]
                if sp["p"] < 58: continue
                if m["state"] == "in" and market_decided(sp["label"], to_int(m["home"]["score"]), to_int(m["away"]["score"])):
                    continue
                cands.append({"code": lg["code"], "lname": lg["sname"], "flag": lg["flag"],
                              "id": m["id"], "day": m["day"], "state": m["state"],
                              "home": m["home"]["name"], "away": m["away"]["name"],
                              "utc": m["utc"], "clock": m.get("clock", ""),
                              "score": f"{m['home']['score']}-{m['away']['score']}" if m["state"] == "in" else None,
                              "label": sp["label"], "p": sp["p"], "stars": sp["stars"], "kind": sp["kind"],
                              "reliab": p["reliab"]})
    cands.sort(key=lambda c: (c["p"] * (0.8 + 0.2 * c["reliab"])), reverse=True)
    # éviter doublons par match
    seen, out = set(), []
    for c in cands:
        if c["id"] in seen: continue
        seen.add(c["id"]); out.append(c)
        if len(out) >= limit: break
    return out

def enrich_match(ev, code):
    m = dict(ev)
    stats = cache_get(f"stats:{code}")
    m["pred"] = None
    m["predPending"] = stats is None
    liveX = None
    # Détails live (stats + momentum + cotes) récupérés par la boucle live
    if ev["state"] == "in":
        liveX = cache_get(f"livedetail:{code}:{ev['id']}")
        if liveX:
            m["liveStats"] = liveX.get("liveStats")
            m["tl_count"] = len(liveX.get("timeline", []))
    if stats is not None:
        try:
            odds_probs = liveX.get("odds_probs") if liveX else None
            inj_n = (len((liveX or {}).get("injuries", {}).get("home", [])),
                     len((liveX or {}).get("injuries", {}).get("away", [])))
            mom = compute_momentum(liveX["liveStats"]) if (liveX and liveX.get("liveStats")) else None
            if ev["state"] == "pre":
                m["pred"] = predict_match(stats, ev["home"]["id"], ev["away"]["id"], absences=inj_n)
            elif ev["state"] == "in":
                minute = (ev["clockSec"] or 0) / 60.0
                m["pred"] = predict_match(stats, ev["home"]["id"], ev["away"]["id"],
                                          minute=minute, score=(to_int(ev["home"]["score"]), to_int(ev["away"]["score"])),
                                          momentum=mom, odds=odds_probs)
            elif ev["state"] == "post" and ev["home"]["score"] is not None:
                m["pred"] = predict_match(stats, ev["home"]["id"], ev["away"]["id"])
        except Exception as ex:
            print(f"[pred] {code}/{ev['id']}: {ex}")
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
               "statsTotal": len(LEAGUES), "topPicks": top, "days": feed_blocks,
               "liveIds": ["%s:%s" % x for x in live_ids]}
    return payload

def refresh_state(force=False):
    payload = build_feed_payload()
    with STATE_LOCK:
        STATE["feed"] = payload
        STATE["version"] += 1
        STATE["liveCount"] = payload["liveCount"]
        STATE["updatedAt"] = payload["generatedAt"]
    sse_broadcast(json.dumps({"type": "feed", "v": STATE["version"], "data": payload}, ensure_ascii=False, separators=(",", ":")))
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
    return {"ok": True, "version": v, "live": lc, "updatedAt": up, "build": "2.1-keepawake",
            "sseClients": len(SSE_CLIENTS),
            "statsReady": sum(1 for lg in LEAGUES if cache_get(f"stats:{lg['code']}") is not None)}

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
                "lineups": None, "odds_probs": None, "seasonStats": None}

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

    # Pronostic + analyse d'expert
    mom = compute_momentum(summ["liveStats"]) if summ.get("liveStats") else None
    minute = (target["clockSec"] or 0) / 60.0 if target["state"] == "in" else None
    score = (to_int(target["home"]["score"]), to_int(target["away"]["score"])) if target["state"] == "in" else None
    inj_n = (len(summ["injuries"]["home"]), len(summ["injuries"]["away"]))
    pred = None
    if home is not None:
        if target["state"] == "in":
            pred = predict_match(stats, target["home"]["id"], target["away"]["id"],
                                 minute=minute, score=score, momentum=mom,
                                 odds=summ.get("odds_probs"), absences=inj_n)
        else:
            # pré-match ET match terminé : prono du modèle (pour analyse / verdict)
            pred = predict_match(stats, target["home"]["id"], target["away"]["id"],
                                 odds=summ.get("odds_probs"), absences=inj_n)

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
# Démarrage
# ---------------------------------------------------------------------------
def warm_stats():
    """Pré-chauffe les stats en arrière-plan."""
    time.sleep(2)
    def st(lg):
        try: build_stats(lg["code"])
        except Exception as e: print(f"[warm] {lg['code']}: {e}")
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(st, LEAGUES))
    print("[warm] toutes les stats sont prêtes")
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

threading.Thread(target=feed_loop, daemon=True).start()
threading.Thread(target=live_detail_loop, daemon=True).start()
threading.Thread(target=warm_stats, daemon=True).start()
threading.Thread(target=keep_awake_loop, daemon=True).start()

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
