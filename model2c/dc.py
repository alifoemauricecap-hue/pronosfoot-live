# -*- coding: utf-8 -*-
"""
DIXON-COLES (§8 MODEL C) — ajustement ML pondéré par le temps
==============================================================
Paramètres : attack/defense par équipe + avantage domicile + rho.
Vraisemblance pondérée exp(-xi * âge_en_jours) (Dixon & Coles 1997).
Identifiabilité : les attaques sont re-centrées (somme nulle) à chaque
évaluation — convention standard, documentée.

xG RÉEL optionnel (C-xG) : chaque match couvert ajoute une
pseudo-observation Poisson des xG (poids dc_xg_weight), méthode marquée
xg_used=True dans les métadonnées. Aucun xG n'est jamais synthétisé.
"""

import math
import numpy as np
from scipy.optimize import minimize
from .availability import parse_ts
from .config2c import CONFIG_2C as C
from .poisson2c import poisson_pmf, score_matrix, clamp


def _log_fact(k):
    return math.lgamma(k + 1)


class DixonColesParams:
    def __init__(self, teams, attack, defense, home_adv, rho, xi, n_train, xg_used):
        self.teams = list(teams)
        self.attack = dict(zip(teams, attack))
        self.defense = dict(zip(teams, defense))
        self.home_adv = float(home_adv)
        self.rho = float(rho)
        self.xi = float(xi)
        self.n_train = int(n_train)
        self.xg_used = bool(xg_used)

    def lambdas(self, home, away):
        ah = self.attack.get(home, 0.0)
        dh = self.defense.get(home, 0.0)
        aa = self.attack.get(away, 0.0)
        da = self.defense.get(away, 0.0)
        lam_h = math.exp(self.home_adv + ah - da)
        lam_a = math.exp(aa - dh)
        return clamp(lam_h, *C["lambda_bounds_home"]), clamp(lam_a, *C["lambda_bounds_away"])

    def predict(self, home, away, max_goals=None):
        lh, la = self.lambdas(home, away)
        m, lh, la = score_matrix(lh, la, max_goals or C["max_goals_grid"], rho=self.rho)
        return m, lh, la


def fit(matches, xi=None, ref_time=None, xg_map=None, xg_weight=None, max_iter=None):
    """Fit Dixon-Coles sur `matches` (train uniquement — le caller garantit
    l'antériorité). matches : dicts {home, away, hg, ag, kickoff_utc}.
    xg_map optionnel : match_id -> (xg_home, xg_away) RÉELS.
    Retourne None si données insuffisantes (honnêteté)."""
    xi = xi if xi is not None else C["dc_xi_default"]
    max_iter = max_iter or C["dc_max_iter"]
    if len(matches) < C["dc_min_train_matches"]:
        return None
    ref = parse_ts(ref_time) if ref_time else max(parse_ts(m["kickoff_utc"]) for m in matches)
    teams = sorted({m["home"] for m in matches} | {m["away"] for m in matches})
    n = len(teams)
    tidx = {t: i for i, t in enumerate(teams)}

    H = np.array([tidx[m["home"]] for m in matches])
    A = np.array([tidx[m["away"]] for m in matches])
    HG = np.array([m["hg"] for m in matches], dtype=int)
    AG = np.array([m["ag"] for m in matches], dtype=int)
    _LOGFACT = np.array([math.lgamma(k + 1) for k in range(30)])
    HGf = _LOGFACT[np.minimum(HG, 29)]
    AGf = _LOGFACT[np.minimum(AG, 29)]
    ages = np.array([(ref - parse_ts(m["kickoff_utc"])).total_seconds() / 86400.0
                     for m in matches], dtype=float)
    W = np.exp(-xi * np.maximum(ages, 0.0))

    # pseudo-observations xG RÉELLES (option)
    xg_rows = []
    xg_weight = C["dc_xg_weight"] if xg_weight is None else xg_weight
    if xg_map and xg_weight > 0:
        cov = 0
        for i, m in enumerate(matches):
            xr = xg_map.get(m.get("match_id"))
            if xr and xr[0] is not None and xr[1] is not None:
                xg_rows.append((H[i], A[i], float(xr[0]), float(xr[1]), W[i] * xg_weight))
                cov += 1
        if cov / len(matches) < C["dc_xg_min_coverage"]:
            xg_rows = []  # couverture insuffisante → poids 0 (honnêteté)
    if xg_rows:
        XH = np.array([r[0] for r in xg_rows]); XA = np.array([r[1] for r in xg_rows])
        XGH = np.array([r[2] for r in xg_rows]); XGA = np.array([r[3] for r in xg_rows])
        XW = np.array([r[4] for r in xg_rows])

    def neg_ll(theta):
        att = theta[:n]
        dfn = theta[n:2 * n]
        ha = theta[2 * n]
        rho = theta[2 * n + 1]
        att = att - att.mean()  # identifiabilité : somme des attaques = 0
        lh = np.exp(ha + att[H] - dfn[A])
        la = np.exp(att[A] - dfn[H])
        # log tau (ajustement scores bas)
        tau = np.ones(len(matches))
        for x, y in ((0, 0), (0, 1), (1, 0), (1, 1)):
            mask = (HG == x) & (AG == y)
            if mask.any():
                if x == 0 and y == 0:
                    tau[mask] = 1.0 - lh[mask] * la[mask] * rho
                elif x == 0 and y == 1:
                    tau[mask] = 1.0 + lh[mask] * rho
                elif x == 1 and y == 0:
                    tau[mask] = 1.0 + la[mask] * rho
                else:
                    tau[mask] = 1.0 - rho
        tau = np.clip(tau, 1e-9, None)
        ll = np.sum(W * (np.log(tau)
                         + HG * np.log(lh) - lh - HGf
                         + AG * np.log(la) - la - AGf))
        if xg_rows:
            lxh = np.exp(ha + att[XH] - dfn[XA])
            lxa = np.exp(att[XA] - dfn[XH])
            # xG continus : approximation Poisson sur valeurs réelles (déclarée)
            ll += np.sum(XW * (XGH * np.log(lxh) - lxh
                               + XGA * np.log(lxa) - lxa))
        # régularisation douce (évite l'explosion des équipes rares)
        ll -= 0.001 * np.sum(att ** 2) + 0.001 * np.sum(dfn ** 2)
        return -ll

    theta0 = np.zeros(2 * n + 2)
    theta0[2 * n] = 0.25     # avantage domicile log initial
    theta0[2 * n + 1] = -0.05  # rho initial
    bounds = [(-3, 3)] * (2 * n) + [(0.0, 1.0), (-0.35, 0.35)]
    res = minimize(neg_ll, theta0, method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": max_iter})
    th = res.x
    att = th[:n] - th[:n].mean()
    params = DixonColesParams(teams, att, th[n:2 * n], th[2 * n], th[2 * n + 1],
                              xi, len(matches), xg_used=bool(xg_rows))
    return params
