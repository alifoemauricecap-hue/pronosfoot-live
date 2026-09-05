# -*- coding: utf-8 -*-
"""
COMPARATEUR 2A vs 2C (§26)
===========================
Compare sur EXACTEMENT les mêmes matchs, avec IC bootstrap appariés.
Le ROI n'est JAMAIS le critère — seules qualité probabiliste et calibration.

Verdict possible : « 2C meilleur » / « 2C équivalent » / « 2C insuffisant » /
« 2C pire » / « 2C échantillon insuffisant ».
"""

from .metrics import metric_bundle, paired_bootstrap_ci
from .config2c import CONFIG_2C as C


def compare(dists_2a, dists_2c, outcomes, label_2a="2A", label_2c="2C"):
    n = len(outcomes)
    b_a = metric_bundle(dists_2a, outcomes, label_2a)
    b_c = metric_bundle(dists_2c, outcomes, label_2c)
    out = {"n": n, label_2a: b_a, label_2c: b_c}
    if n < C["compare_min_n"]:
        out["verdict"] = "2C échantillon insuffisant"
        return out
    ci_ll = paired_bootstrap_ci(dists_2a, dists_2c, outcomes, stat="logloss")
    ci_br = paired_bootstrap_ci(dists_2a, dists_2c, outcomes, stat="brier")
    out["delta_logloss"] = ci_ll   # delta = stat(2A) − stat(2C) ; >0 ⇒ 2C meilleur
    out["delta_brier"] = ci_br
    ll_better = ci_ll.get("significant") and ci_ll["delta"] > 0
    br_better = ci_br.get("significant") and ci_br["delta"] > 0
    ll_worse = ci_ll.get("significant") and ci_ll["delta"] < 0
    br_worse = ci_br.get("significant") and ci_br["delta"] < 0
    if ll_better and br_better:
        out["verdict"] = "2C meilleur"
    elif ll_worse or br_worse:
        out["verdict"] = "2C pire"
    elif ll_better or br_better:
        out["verdict"] = "2C équivalent" if n >= C["strong_claim_n"] else "2C insuffisant"
    else:
        out["verdict"] = "2C équivalent" if n >= C["strong_claim_n"] else "2C insuffisant"
    return out
