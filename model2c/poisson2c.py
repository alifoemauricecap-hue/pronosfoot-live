# -*- coding: utf-8 -*-
"""
MATHÉMATIQUES PROBABILISTES 2C — PURESS, SANS I/O
=================================================
Implémentation INDÉPENDANTE de engine.py (2A) : 2C ne doit jamais subir
une modification de 2A, ni l'inverse. Poisson PMF, matrice de score,
marchés 1N2/DC/O-U/BTTS, ajustement bas-score Dixon-Coles (tau).
"""

import math


def poisson_pmf(lam, n=10):
    """Distribution de Poisson 0..n (récurrence). lam>0."""
    if lam <= 0:
        raise ValueError("lambda doit être > 0")
    ps = [math.exp(-lam)]
    for i in range(1, n + 1):
        ps.append(ps[-1] * lam / i)
    return ps


def clamp(x, lo, hi):
    return min(max(x, lo), hi)


def score_matrix(lam_h, lam_a, max_goals=10, rho=None,
                 bounds_h=(0.10, 4.5), bounds_a=(0.08, 4.0)):
    """Matrice de score P(i,j) ; ajustement Dixon-Coles si rho donné.
    Retourne matrice normalisée (somme 1) + lambdas bornés."""
    lam_h = clamp(lam_h, *bounds_h)
    lam_a = clamp(lam_a, *bounds_a)
    ph, pa = poisson_pmf(lam_h, max_goals), poisson_pmf(lam_a, max_goals)
    m = [[ph[i] * pa[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    if rho is not None:
        m[0][0] *= dc_tau(0, 0, lam_h, lam_a, rho)
        m[0][1] *= dc_tau(0, 1, lam_h, lam_a, rho)
        m[1][0] *= dc_tau(1, 0, lam_h, lam_a, rho)
        m[1][1] *= dc_tau(1, 1, lam_h, lam_a, rho)
    tot = sum(sum(r) for r in m)
    if tot <= 0:
        raise ValueError("matrice dégénérée")
    return [[v / tot for v in r] for r in m], lam_h, lam_a


def dc_tau(x, y, lam_h, lam_a, rho):
    """Facteur de dépendance Dixon-Coles pour scores bas (0-0, 0-1, 1-0, 1-1)."""
    if x == 0 and y == 0:
        return 1.0 - lam_h * lam_a * rho
    if x == 0 and y == 1:
        return 1.0 + lam_h * rho
    if x == 1 and y == 0:
        return 1.0 + lam_a * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def markets_from_matrix(m):
    """Agrégats de marché depuis une matrice symétrique de scores.
    Retourne probabilités BRUTES dans [0,1] : 1N2, DC, O1.5/2.5/3.5, BTTS,
    plus table des scores exacts (top)."""
    p1 = px = p2 = o15 = o25 = o35 = btts = 0.0
    cells = []
    n = len(m) - 1
    for i in range(n + 1):
        for j in range(n + 1):
            p = m[i][j]
            if i > j:
                p1 += p
            elif i == j:
                px += p
            else:
                p2 += p
            if i + j >= 2:
                o15 += p
            if i + j >= 3:
                o25 += p
            if i + j >= 4:
                o35 += p
            if i > 0 and j > 0:
                btts += p
            cells.append((p, i, j))
    cells.sort(reverse=True)
    return {
        "1": p1, "N": px, "2": p2,
        "1N": p1 + px, "N2": px + p2, "12": p1 + p2,
        "O1.5": o15, "U1.5": 1 - o15,
        "O2.5": o25, "U2.5": 1 - o25,
        "O3.5": o35, "U3.5": 1 - o35,
        "BTTS_YES": btts, "BTTS_NO": 1 - btts,
        "top_scores": [(f"{i}-{j}", p) for p, i, j in cells[:5]],
    }


def distributions_1n2(markets):
    return {"1": markets["1"], "N": markets["N"], "2": markets["2"]}
