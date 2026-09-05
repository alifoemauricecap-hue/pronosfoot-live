# -*- coding: utf-8 -*-
"""
FEATURE STORE / FEATURE ENGINE (§4)
====================================
RAW PIT DATA → NORMALISATION → FEATURES → MODEL.
Chaque feature est : déterministe, reproductible, temporelle (as_of),
traçable (source), versionnée (feature_registry + FEATURE_VERSION).

Chaque feature sait répondre : « pourquoi cette valeur existait-elle à T ? »
via les champs d'audit du vecteur (as_of, fenêtre, sources, UNKNOWN explicites).
"""

import hashlib
import json
from .availability import parse_ts
from .config2c import CONFIG_2C as C, FEATURE_VERSION

UNKNOWN = None  # convention : UNKNOWN est None en JSON — JAMAIS 0.0 inventé

# Registre des features : nom → métadonnées (description, sources, requis)
FEATURE_REGISTRY = {
    "elo_home": {"desc": "rating Elo pré-match domicile", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "elo_away": {"desc": "rating Elo pré-match extérieur", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "elo_diff": {"desc": "diff Elo incluant avantage domicile", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "form_pts_w{W}": {"desc": "points/match sur les W derniers matchs", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "form_gf_w{W}": {"desc": "buts marqués/match sur W", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "form_ga_w{W}": {"desc": "buts encaissés/match sur W", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "form_cs_w{W}": {"desc": "clean sheets/match sur W", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "form_fts_w{W}": {"desc": "matchs sans marquer/match sur W", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "rest_days": {"desc": "jours depuis le dernier match", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "matches_7d": {"desc": "matchs joués sur 7 jours", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "matches_14d": {"desc": "matchs joués sur 14 jours (congestion)", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "opp_strength_5": {"desc": "Elo moyen des 5 derniers adversaires", "src": ["openligadb"], "kind": "REAL_DERIVED"},
    "xg_for_rate": {"desc": "xG pour/match (RÉEL StatsBomb)", "src": ["statsbomb_open"], "kind": "REAL"},
    "xg_against_rate": {"desc": "xG contre/match (RÉEL StatsBomb)", "src": ["statsbomb_open"], "kind": "REAL"},
    "weather": {"desc": "météo — NON implémentée : venues non semés", "src": [], "kind": "UNKNOWN"},
    "lineups": {"desc": "compositions historiques — indisponibles", "src": [], "kind": "UNKNOWN"},
    "injuries": {"desc": "absences historiques datées — indisponibles", "src": [], "kind": "UNKNOWN"},
    "odds_timed": {"desc": "cotes horodatées — football-data en panne 503", "src": [], "kind": "UNKNOWN"},
}


class TeamHistory:
    """Historique chronologique d'une équipe (matchs passés uniquement)."""
    __slots__ = ("name", "matches")

    def __init__(self, name):
        self.name = name
        self.matches = []  # dicts {day(datetime), gf, ga, loc('H'/'A'), pts, opp, opp_elo}

    def add(self, day, gf, ga, loc, opp, opp_elo=None):
        pts = 3 if gf > ga else (1 if gf == ga else 0)
        self.matches.append({"day": day, "gf": gf, "ga": ga, "loc": loc,
                             "pts": pts, "opp": opp,
                             "cs": 1 if ga == 0 else 0, "fts": 1 if gf == 0 else 0,
                             "opp_elo": opp_elo})

    def last_w(self, w, as_of, loc=None):
        sel = [m for m in self.matches if m["day"] < as_of and (loc is None or m["loc"] == loc)]
        sel.sort(key=lambda m: m["day"], reverse=True)
        return sel[:w]


def _avg(rows, key):
    if not rows:
        return None
    if len(rows) < 1:
        return None
    return round(sum(r[key] for r in rows) / len(rows), 6)


def form_features(history, as_of, loc=None):
    """Fenêtres 3/5/8/10/15 (§5). Insuffisant → UNKNOWN (None), jamais 0."""
    feats = {}
    for w in C["form_windows"]:
        rows = history.last_w(w, as_of, loc)
        if len(rows) < max(1, w // 2 if w > 3 else 1):
            # fenêtre trop vide → UNKNOWN explicite (honnêteté, pas de 0)
            feats[f"form_pts_w{w}"] = None
            feats[f"form_gf_w{w}"] = None
            feats[f"form_ga_w{w}"] = None
            feats[f"form_cs_w{w}"] = None
            feats[f"form_fts_w{w}"] = None
            feats[f"form_n_w{w}"] = len(rows)
            continue
        feats[f"form_pts_w{w}"] = _avg(rows, "pts")
        feats[f"form_gf_w{w}"] = _avg(rows, "gf")
        feats[f"form_ga_w{w}"] = _avg(rows, "ga")
        feats[f"form_cs_w{w}"] = _avg(rows, "cs")
        feats[f"form_fts_w{w}"] = _avg(rows, "fts")
        feats[f"form_n_w{w}"] = len(rows)
    return feats


def calendar_features(history, as_of):
    """Repos / congestion (§11) — dérivés des dates RÉELLES. Jamais inventés."""
    past = [m for m in history.matches if m["day"] < as_of]
    if not past:
        return {"rest_days": None, "matches_7d": 0, "matches_14d": 0}
    past.sort(key=lambda m: m["day"], reverse=True)
    last = past[0]["day"]
    rest = (as_of - last).total_seconds() / 86400.0
    m7 = sum(1 for m in past if (as_of - m["day"]).total_seconds() < 7 * 86400)
    m14 = sum(1 for m in past if (as_of - m["day"]).total_seconds() < 14 * 86400)
    return {"rest_days": round(min(rest, 30.0), 3), "matches_7d": m7, "matches_14d": m14}


def opponent_strength(history, as_of, n=5):
    """Elo moyen des n derniers adversaires (§5 « force des adversaires »)."""
    rows = history.last_w(n, as_of)
    elos = [r["opp_elo"] for r in rows if r.get("opp_elo") is not None]
    if not elos:
        return None
    return round(sum(elos) / len(elos), 3)


def feature_hash(feats, as_of, feature_version=FEATURE_VERSION):
    """Hash SHA-256 du vecteur (canonique) — reproductibilité §29."""
    canon = json.dumps({"as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
                        "version": feature_version,
                        "feats": feats}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def build_match_features(match, as_of, elo_feats, strength_feats, hist_home, hist_away,
                         xg_for_home=None, xg_against_home=None,
                         xg_for_away=None, xg_against_away=None):
    """Vecteur complet d'un match à T=as_of. Les données passées en argument
    doivent DÉJÀ être filtrées par is_available_at (P0)."""
    t = parse_ts(as_of)
    f = {}
    f.update({f"elo_{k[4:]}": v for k, v in elo_feats.items()})
    f.update(strength_feats)
    for side, hist in (("home", hist_home), ("away", hist_away)):
        ff = form_features(hist, t)
        f.update({f"{side}_{k}": v for k, v in ff.items()})
        loc = "H" if side == "home" else "A"
        lf = form_features(hist, t, loc)
        f.update({f"{side}_loc_{k}": v for k, v in lf.items()})
        cf = calendar_features(hist, t)
        f.update({f"{side}_{k}": v for k, v in cf.items()})
        f[f"{side}_opp_strength_5"] = opponent_strength(hist, t)
    # xG RÉEL ou UNKNOWN (None) — jamais dérivé
    f["home_xg_for_rate"] = xg_for_home
    f["home_xg_against_rate"] = xg_against_home
    f["away_xg_for_rate"] = xg_for_away
    f["away_xg_against_rate"] = xg_against_away
    f["_feature_version"] = FEATURE_VERSION
    f["_as_of"] = t.isoformat()
    srcs = {"openligadb"}
    if xg_for_home is not None or xg_for_away is not None:
        srcs.add("statsbomb_open")
    f["_sources"] = sorted(srcs)
    f["feature_hash"] = feature_hash({k: v for k, v in f.items()
                                      if not k.startswith("_") and k != "feature_hash"}, t)
    return f
