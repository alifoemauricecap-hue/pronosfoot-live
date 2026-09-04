# -*- coding: utf-8 -*-
"""
MOTEUR DE PRÉDICTION (code déplacé depuis app.py — AUCUN changement de calcul)
=============================================================================
Ce module contient UNIQUEMENT des fonctions pures (sans réseau, sans BD) :

- modèle de Poisson pondéré par récence (fenêtre ~150 jours, demi-vie 70 j),
- forces attaque/défense domicile/extérieur avec contraction (shrinkage k=3),
- ajustement absences, momentum live, fusion adaptative des cotes,
- construction des 12 marchés affichés et du « pari le plus sûr »,
- OUTILS D'ÉVALUATION HONNÊTES : regroupement des marchés en familles
  (1N2, DC, O1.5, O2.5, O3.5, BTTS), calcul gagné/perdu, Brier, LogLoss.

Toute prédiction publiée référence MODEL_NAME / MODEL_VERSION / MODEL_CONFIG :
une ancienne prédiction restera liée à « poisson 1.0.0 » même quand le modèle
évoluera (ÉTAPE 2C : calibration, Dixon-Coles...).
"""

import math

# ---------------------------------------------------------------------------
# Identité du modèle (versionnage obligatoire — ÉTAPE 2A §16)
# ---------------------------------------------------------------------------
MODEL_NAME = "poisson"
MODEL_VERSION = "1.0.0"

# Configuration exacte du modèle 1.0.0 (hachée dans chaque snapshot).
MODEL_CONFIG = {
    "window_days": 150,          # historique utilisé
    "recency_half_life_days": 70,
    "shrinkage_k": 3.0,
    "home_away_split": 0.65,     # poids stats dom/ext vs globales
    "absence_penalty": 0.025,    # -2,5 % par absence majeure (plafond 4)
    "absence_cap": 4,
    "momentum_mult_bounds": [0.5, 1.75],
    "odds_blend_base": 0.25, "odds_blend_extra": 0.45, "odds_blend_cap": 0.65,
    "lambda_bounds_home": [0.15, 4.5],
    "lambda_bounds_away": [0.10, 4.0],
    "max_goals_grid": 10,
    "value_edge_min": 0.08,
    # AVERTISSEMENT (ÉTAPE 2A §28) : probabilités NON calibrées.
    "calibrated": False,
}

# Familles de marchés évaluées (une ligne de prédiction par famille et par
# version gelée). Les 12 lignes d'affichage frontend sont reconstruites depuis
# ces 6 distributions.
MARKET_FAMILIES = ("1N2", "DC", "O1.5", "O2.5", "O3.5", "BTTS")


def poisson(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def poisson_pmf(lam, n=10):
    """Distribution de Poisson complète 0..n par récurrence — beaucoup plus
    rapide que factorielle à chaque appel (gain CPU x10 sur serveur mutualisé)."""
    ps = [math.exp(-lam)]
    for i in range(1, n + 1):
        ps.append(ps[-1] * lam / i)
    return ps


def team_rates(t, loc):
    if t["sw"] <= 0.05:
        return None, None
    gf_overall, ga_overall = t["gf"] / t["sw"], t["ga"] / t["sw"]
    if t[f"{loc}_n"] >= 3 and t[f"{loc}_sw"] > 0.3:
        return 0.65 * (t[f"{loc}_gf"] / t[f"{loc}_sw"]) + 0.35 * gf_overall, \
               0.65 * (t[f"{loc}_ga"] / t[f"{loc}_sw"]) + 0.35 * ga_overall
    return gf_overall, ga_overall


def shrink(avg, league_avg, sw, k=3.0):
    return (avg * sw + league_avg * k) / (sw + k)


def american_to_prob(ml):
    try:
        ml = float(ml)
    except Exception:
        return None
    if ml == 0:
        return None
    if ml > 0:
        return 100.0 / (ml + 100.0)
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
    """Retourne probabilités complètes + marchés + pari le plus sûr.
    Fonction PURE : aucune lecture réseau/BD, aucun effet de bord."""
    lg = stats["league"]
    home, away = stats["teams"].get(home_id), stats["teams"].get(away_id)
    if home is None or away is None:
        return None

    h_att, h_def = team_rates(home, "home")
    a_att, a_def = team_rates(away, "away")
    reliab = min(1.0, (home["sw"] + away["sw"]) / 14.0)
    if h_att is None:
        h_att = lg["homeAvg"]
    if a_att is None:
        a_att = lg["awayAvg"]
    if h_def is None:
        h_def = lg["homeAvg"]
    if a_def is None:
        a_def = lg["awayAvg"]

    h_att = shrink(h_att, lg["homeAvg"], home["sw"])
    a_def = shrink(a_def, lg["awayAvg"], away["sw"])
    a_att = shrink(a_att, lg["awayAvg"], away["sw"])
    h_def = shrink(h_def, lg["homeAvg"], home["sw"])

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
        lam_h = 1e-6 if remaining <= 0.01 else lam_h * remaining
        lam_a = 1e-6 if remaining <= 0.01 else lam_a * remaining
        # Momentum live : domination mesurée sur les stats du match
        if momentum:
            dom_h, dom_a = momentum  # 0..1 chacun (somme ~1)
            lam_h *= min(max(0.35 + 1.3 * dom_h, 0.5), 1.75)
            lam_a *= min(max(0.35 + 1.3 * dom_a, 0.5), 1.75)

    MAXG = 10
    p1 = px = p2 = over15 = over25 = over35 = btts = 0.0
    cells = []
    dist_h, dist_a = poisson_pmf(lam_h, MAXG), poisson_pmf(lam_a, MAXG)
    for i in range(MAXG + 1):
        pi = dist_h[i]
        for j in range(MAXG + 1):
            p = pi * dist_a[j]
            fh, fa = cur_h + i, cur_a + j
            if fh > fa:
                p1 += p
            elif fh == fa:
                px += p
            else:
                p2 += p
            tg = i + j
            if tg > 1.5:
                over15 += p
            if tg > 2.5:
                over25 += p
            if tg > 3.5:
                over35 += p
            if (cur_h > 0 or i > 0) and (cur_a > 0 or j > 0):
                btts += p
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
            odds_info = {"p1": round(mh * 100), "px": round(md * 100), "p2": round(ma * 100), "w": round(w * 100),
                         "raw": (mh, md, ma)}

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

    # Détection de VALUE BET : l'IA est nettement plus confiante que le marché
    value = None
    if odds_info and not live:
        mh, md, ma = odds_info["raw"]
        for side, pm, mk in (("1", p1, mh), ("N", px, md), ("2", p2, ma)):
            edge = pm - mk
            if edge >= 0.08 and pm >= 0.35 and (value is None or edge > value[1]):
                value = (side, round(edge * 100))
    odds_info_out = None
    if odds_info:
        odds_info_out = {k: v for k, v in odds_info.items() if k != "raw"}

    res = {"p1": round(p1 * 100, 1), "px": round(px * 100, 1), "p2": round(p2 * 100, 1),
           "pick": "1" if p1 >= px and p1 >= p2 else ("N" if px >= p2 else "2"),
           "topScores": [{"s": f"{fh}-{fa}", "p": round(p * 100, 1)} for p, fh, fa in cells[:5]],
           "over15": round(over15 * 100, 1), "over25": round(over25 * 100, 1), "over35": round(over35 * 100, 1),
           "under25": round((1 - over25) * 100, 1), "under35": round((1 - over35) * 100, 1),
           "btts": round(btts * 100, 1),
           "markets": [{"label": l, "p": round(p * 100, 1), "kind": k} for l, p, k in markets],
           "safePick": {"label": safe[0], "p": round(safe[2] * 100, 1), "stars": safe_stars, "kind": safe[3]},
           "xgH": round(lam_h, 2), "xgA": round(lam_a, 2),
           "stars": stars, "reliab": round(reliab, 2), "live": live,
           "marketOdds": odds_info_out}
    if value:
        res["valueBet"] = {"side": value[0], "edge": value[1]}
    return res


# ---------------------------------------------------------------------------
# ÉVALUATION HONNÊTE DES MARCHÉS (utilisée UNIQUEMENT au règlement, jamais
# pour recalculer une prédiction — ÉTAPE 2A §9)
# ---------------------------------------------------------------------------
def family_distributions(pred):
    """Convertit la sortie moteur en 6 distributions par famille de marché.
    Chaque distribution somme à 100 % (tolérance numérique flottante)."""
    p1, px, p2 = pred["p1"] / 100.0, pred["px"] / 100.0, pred["p2"] / 100.0
    over15, over25, over35 = pred["over15"] / 100.0, pred["over25"] / 100.0, pred["over35"] / 100.0
    btts = pred["btts"] / 100.0
    return {
        "1N2": {"1": p1, "N": px, "2": p2},
        # DC : trois combinaisons NON exclusives (1N et N2 gagnent toutes deux
        # sur un nul) — ce sont des probabilités de combinaisons, pas une
        # distribution exclusive ; l'évaluation est BINAIRE (voir settle_family).
        "DC": {"1N": min(1.0, p1 + px), "N2": min(1.0, px + p2), "12": min(1.0, p1 + p2)},
        "O1.5": {"PLUS": over15, "MOINS": 1 - over15},
        "O2.5": {"PLUS": over25, "MOINS": 1 - over25},
        "O3.5": {"PLUS": over35, "MOINS": 1 - over35},
        "BTTS": {"OUI": btts, "NON": 1 - btts},
    }


def family_pick(family, dist):
    """Sélection = issue la plus probable de la famille."""
    return max(dist.items(), key=lambda kv: kv[1])[0]


def family_outcome(family, hg, ag):
    """Libellé d'issue réelle au regard du score final (informatif ; pour DC le
    match nul rend 1N ET N2 gagnants → libellé NUL, évaluation binaire dédiée)."""
    tot = hg + ag
    if family == "1N2":
        return "1" if hg > ag else ("N" if hg == ag else "2")
    if family == "DC":
        return "1N" if hg > ag else ("N2" if hg < ag else "NUL")
    if family == "O1.5":
        return "PLUS" if tot >= 2 else "MOINS"
    if family == "O2.5":
        return "PLUS" if tot >= 3 else "MOINS"
    if family == "O3.5":
        return "PLUS" if tot >= 4 else "MOINS"
    if family == "BTTS":
        return "OUI" if (hg > 0 and ag > 0) else "NON"
    return None


_EPS = 1e-12


def multiclass_brier(dist, outcome):
    """Brier multi-classes : somme des (p_i − o_i)². 0 = parfait, 2 = pire."""
    keys = set(dist) | {outcome}
    tot = 0.0
    for k in keys:
        o = 1.0 if k == outcome else 0.0
        tot += (dist.get(k, 0.0) - o) ** 2
    return tot


def multiclass_logloss(dist, outcome):
    """LogLoss sur la probabilité publiée de l'issue réelle (bornée 1e-12)."""
    p = min(max(dist.get(outcome, 0.0), _EPS), 1.0)
    return -math.log(p)


def settle_family(family, selection, dist, hg, ag):
    """Règle un marché gelé : (libellé_d_issue, gagné, brier, logloss).

    - 1N2 : issues EXCLUSIVES → Brier multi-classes et LogLoss sur la
      distribution complète {1,N,2} conservée à la publication (§13).
    - DC / O-U / BTTS : chaque ligne gelée est une SÉLECTION BINAIRE →
      Brier binaire (p−o)² + (1−p−(1−o))² ∈ [0,2] et LogLoss binaire sur la
      probabilité publiée de la sélection. DC nul : « 1N » et « N2 » gagnent
      tous les deux (legs non exclusives).
    """
    if family == "1N2":
        outcome = family_outcome(family, hg, ag)
        if outcome is None:
            return None, None, None, None
        won = selection == outcome
        return outcome, won, multiclass_brier(dist, outcome), multiclass_logloss(dist, outcome)

    def leg_won(sel):
        if sel == "1N":
            return hg >= ag
        if sel == "N2":
            return hg <= ag
        if sel == "12":
            return hg != ag
        if sel == "PLUS":
            return hg + ag >= {"O1.5": 2, "O2.5": 3, "O3.5": 4}.get(family, 999)
        if sel == "MOINS":
            return hg + ag < {"O1.5": 2, "O2.5": 3, "O3.5": 4}.get(family, -1)
        if sel == "OUI":
            return hg > 0 and ag > 0
        if sel == "NON":
            return not (hg > 0 and ag > 0)
        return None

    won = leg_won(selection)
    if won is None:
        return None, None, None, None
    p = min(max(dist.get(selection, 0.0), _EPS), 1.0)
    o = 1.0 if won else 0.0
    brier = (p - o) ** 2 + ((1 - p) - (1 - o)) ** 2
    ll = -math.log(p if won else (1.0 - p))
    return family_outcome(family, hg, ag), won, brier, ll


def market_decided(label, hg, ag):
    """Un marché live est-il déjà tranché (gagné ou perdu d'avance) ?"""
    def to_float(x, default=0.0):
        try:
            return float(x)
        except Exception:
            return default
    tot = hg + ag
    if "marquent : OUI" in label:
        return hg > 0 and ag > 0
    if "marquent : NON" in label:
        return False
    if label.startswith("Plus de"):
        thr = to_float(label.split("Plus de ")[1].split(" but")[0].replace(",", "."), 99)
        return tot > thr + 0.5
    if label.startswith("Moins de"):
        thr = to_float(label.split("Moins de ")[1].split(" but")[0].replace(",", "."), 99)
        return tot > thr
    return False
