# -*- coding: utf-8 -*-
"""
ÉTAPE 2C — COMPARATEUR SUR PRÉDICTIONS 2A RÉELLES DE PRODUCTION
================================================================
Entrées : backup production (lecture seule, jamais la prod directement) +
          dataset OL (data/2c_offline/ol_matches.json).
Méthode : version d'évaluation 2A (dernière gelée AVANT kickoff — sémantique
          repo.evaluation_version) vs 2C (features OL passées strictement) sur
          EXACTEMENT les mêmes matchs. N affiché ; verdict honnête quel qu'il
          soit (échantillon insuffisant probable à date — déclaré).

Usage : python scripts/2c_prod_compare.py /tmp/prod2.sqlite
"""
import gzip
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GH_BACKUP", "0")
os.environ["PRONOFOOT_NO_THREADS"] = "1"

from model2c.config2c import CONFIG_2C as C
from model2c.identity import canonical_team
from model2c.pipeline import run_pass
from model2c.walkforward import Fold
from model2c.compare import compare
from model2c.metrics import metric_bundle

GER_LEAGUE = {"ger.1": "bl1", "ger.2": "bl2"}


def _open_db(path):
    if path.endswith(".gz"):
        dst = path[:-3]
        with gzip.open(path, "rb") as src, open(dst, "wb") as out:
            out.write(src.read())
        path = dst
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def settled_ger_1n2(con):
    """Version d'évaluation par match : latest gelée AVANT le coup d'envoi."""
    rows = con.execute("""
        SELECT p.match_id, p.version_seq, p.frozen_at, p.kickoff_time_utc,
               p.distribution_json, m.competition, m.home_team, m.away_team,
               r.home_score, r.away_score
        FROM predictions p
        JOIN matches m ON m.id = p.match_id
        JOIN results r ON r.match_id = p.match_id
        WHERE p.market='1N2' AND m.competition IN ('ger.1','ger.2')
        ORDER BY p.kickoff_time_utc""").fetchall()
    by_match = {}
    for r in rows:
        if r["frozen_at"] > r["kickoff_time_utc"]:
            continue  # la version d'évaluation n'est JAMAIS post-kickoff
        cur = by_match.get(r["match_id"])
        if not cur or r["version_seq"] >= cur["version_seq"]:
            by_match[r["match_id"]] = dict(r)
    return list(by_match.values())


def main():
    backup = sys.argv[1] if len(sys.argv) > 1 else "/tmp/prod2.sqlite"
    con = _open_db(backup)
    rows = settled_ger_1n2(con)
    overlap = []
    skipped = []
    for r in rows:
        h, a = canonical_team(r["home_team"]), canonical_team(r["away_team"])
        if not h or not a or r["competition"] not in GER_LEAGUE:
            skipped.append({"match_id": r["match_id"], "home": r["home_team"],
                            "away": r["away_team"], "reason": "identity UNKNOWN"})
            continue
        overlap.append({
            "match_id": "prod:" + r["match_id"], "league": GER_LEAGUE[r["competition"]],
            "season": 2026, "kickoff_utc": r["kickoff_time_utc"],
            "home": h, "away": a, "hg": r["home_score"], "ag": r["away_score"],
            "effective_at": r["kickoff_time_utc"],
            "2a_dist": json.loads(r["distribution_json"]),
            "2a_frozen_at": r["frozen_at"],
            "home_name_espn": r["home_team"], "away_name_espn": r["away_team"]})

    print(f"[2c-prod] matchs ger avec résultat+1N2 gelée : {len(rows)}")
    print(f"[2c-prod] overlap cartographiable : {len(overlap)} | identité UNKNOWN : {len(skipped)}")

    with open(os.path.join(C["offline_dir"], "ol_matches.json"), encoding="utf-8") as f:
        ol = json.load(f)["matches"]
    train = [m for m in ol if m["season"] < 2026]
    fold = Fold("prod_overlap_sept2026", train, overlap)
    hp = json.load(open(os.path.join(C["offline_dir"], "report_2c.json")))["hyperparams"]
    recs = run_pass(train + overlap, [fold], elo_k=hp["elo_k"],
                    dc_xi=hp["dc_xi"], weight_c=hp["weight_c"], fit_dc=True)

    d2a, dB, dC, dD, outs = [], [], [], [], []
    table = []
    for r, o in zip(recs, overlap):
        if r.get("prediction_status") != "OK" or "2A" not in r:
            continue
        # la distribution 2A r provient du rejeu OL — ici on veut la VRAIE 2A prod
        rec2a = {"dist": {k: float(v) for k, v in o["2a_dist"].items()}}
        pts = {"id": r["match_id"], "home": o["home_name_espn"], "away": o["away_name_espn"],
               "ko": o["kickoff_utc"], "score": f"{o['hg']}-{o['ag']}", "outcome": r["outcome"]}
        for model, key in (("B", "B"), ("C", "C"), ("D", "D")):
            if key in r:
                pts[key] = {k: round(v, 4) for k, v in r[key]["dist"].items()}
        pts["2A_prod"] = rec2a["dist"]
        d2a.append(rec2a["dist"])
        if "B" in r:
            dB.append(r["B"]["dist"])
        if "C" in r:
            dC.append(r["C"]["dist"])
        if "D" in r:
            dD.append(r["D"]["dist"])
        outs.append(r["outcome"])
        table.append(pts)

    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "note": "2A = prédictions RÉELLES gelées en production (backup, lecture seule) ; "
                   "2C = mêmes matchs, features OL passées strictement. N affiché, verdict honnête.",
           "n": len(outs), "hyperparams": hp,
           "bundles": {"2A_prod": metric_bundle(d2a, outs, "2A_prod")},
           "matches": table}
    if dB:
        out["compare_B"] = compare(d2a, dB, outs, "2A_prod", "B")
    if dC:
        out["compare_C"] = compare(d2a, dC, outs, "2A_prod", "C")
    if dD:
        out["compare_D"] = compare(d2a, dD, outs, "2A_prod", "D")
    path = os.path.join(C["offline_dir"], "report_2c_prod_overlap.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"→ {path}")
    for k in ("compare_B", "compare_C", "compare_D"):
        if k in out:
            print(f"[2c-prod] {k} : {out[k]['verdict']} (n={out[k]['n']})")


if __name__ == "__main__":
    main()
