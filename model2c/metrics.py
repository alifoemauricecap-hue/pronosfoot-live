# -*- coding: utf-8 -*-
"""
MÉTRIQUES PROBABILISTES (§18/§19/§20)
======================================
Brier multi-classes, LogLoss, accuracy, ECE, tables de fiabilité.
RÈGLES : chaque métrique porte N ; sous minimum_sample_size → marquée
INSUFFICIENT_SAMPLE ; intervalles d'incertitude par bootstrap apparié.
"""

import math
import random
from .config2c import CONFIG_2C as C

_EPS = 1e-12


def brier_multi(dist, outcome):
    keys = set(dist) | {outcome}
    return sum((dist.get(k, 0.0) - (1.0 if k == outcome else 0.0)) ** 2 for k in keys)


def logloss_multi(dist, outcome):
    p = min(max(dist.get(outcome, 0.0), _EPS), 1.0)
    return -math.log(p)


def accuracy_argmax(dist, outcome):
    return 1 if max(dist.items(), key=lambda kv: kv[1])[0] == outcome else 0


def ece_binary(probs, outcomes, bins=None):
    """Expected Calibration Error (binaire) : moyenne des |avg_p - freq| pondérée."""
    bins = bins or C["ece_bins"]
    n = len(probs)
    if n == 0:
        return None
    tot = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, o) for p, o in zip(probs, outcomes)
               if (lo <= p < hi) or (b == bins - 1 and p == 1.0)]
        if not sel:
            continue
        avgp = sum(p for p, _ in sel) / len(sel)
        freq = sum(o for _, o in sel) / len(sel)
        tot += (len(sel) / n) * abs(avgp - freq)
    return tot


def reliability_table(probs, outcomes, bins=None):
    bins = bins or C["ece_bins"]
    rows = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, o) for p, o in zip(probs, outcomes)
               if (lo <= p < hi) or (b == bins - 1 and p == 1.0)]
        if sel:
            rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(sel),
                         "avg_predicted": round(sum(p for p, _ in sel) / len(sel), 4),
                         "observed_freq": round(sum(o for _, o in sel) / len(sel), 4)})
    return rows


def multiclass_view(dists, outcomes):
    """Vue binaire 'issue argmax réalisée ?' utilisée pour ECE/fiabilité 1N2."""
    probs = [max(d.values()) for d in dists]
    outs = [1 if max(d.items(), key=lambda kv: kv[1])[0] == o else 0
            for d, o in zip(dists, outcomes)]
    return probs, outs


def metric_bundle(dists, outcomes, label=None):
    """Bundle complet 1N2 avec N systématique (§19)."""
    n = len(dists)
    if n == 0:
        return {"label": label, "n": 0, "sample_verdict": "INSUFFICIENT_SAMPLE"}
    brier = sum(brier_multi(d, o) for d, o in zip(dists, outcomes)) / n
    ll = sum(logloss_multi(d, o) for d, o in zip(dists, outcomes)) / n
    acc = sum(accuracy_argmax(d, o) for d, o in zip(dists, outcomes)) / n
    probs, outs = multiclass_view(dists, outcomes)
    verdict = "OK" if n >= C["strong_claim_n"] else (
        "WEAK_SAMPLE" if n >= C["minimum_sample_size"] else "INSUFFICIENT_SAMPLE")
    return {"label": label, "n": n, "brier": round(brier, 5),
            "logloss": round(ll, 5), "accuracy": round(acc, 4),
            "ece": round(ece_binary(probs, outs), 5),
            "reliability": reliability_table(probs, outs),
            "sample_verdict": verdict}


def paired_bootstrap_ci(dists_a, dists_b, outcomes, stat="logloss", n_boot=None, seed=None):
    """IC 95 % de (stat(A) − stat(B)) par bootstrap APPARIÉ sur les mêmes
    matchs (§20) : la comparaison est statistiquement honnête."""
    n_boot = n_boot or C["bootstrap_n"]
    rng = random.Random(seed if seed is not None else C["bootstrap_seed"])
    n = len(outcomes)
    if n < C["compare_min_n"]:
        return {"verdict": "INSUFFICIENT_SAMPLE", "n": n}
    fn = logloss_multi if stat == "logloss" else brier_multi
    diffs = [fn(a, o) - fn(b, o) for a, b, o in zip(dists_a, dists_b, outcomes)]
    boots = []
    for _ in range(n_boot):
        s = sum(diffs[rng.randrange(n)] for _ in range(n)) / n
        boots.append(s)
    boots.sort()
    lo = boots[int(0.025 * n_boot)]
    hi = boots[int(0.975 * n_boot)]
    mean = sum(diffs) / n
    return {"stat": stat, "n": n, "delta": round(mean, 5),
            "ci95": [round(lo, 5), round(hi, 5)],
            "significant": bool(hi < 0 or lo > 0),
            "better": "A" if hi < 0 else ("B" if lo > 0 else None)}
