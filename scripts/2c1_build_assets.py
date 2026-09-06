# -*- coding: utf-8 -*-
"""
ÉTAPE 2C.1 — CONSTRUCTION DES ASSETS PROD (offline, UNE SEULE FOIS)
====================================================================
Produit les assets FIGÉS utilisés par le shadow en production :

- model2c/prod_assets/seed_bl_history.json   (6 120 matchs RÉELS OL 2016→2025)
- model2c/prod_assets/calibration_2c_v1.json (Platt entraîné sur l'historique
  réel évaluable 2019→2025 — passé STRICT par rapport à la prod ; §10 :
  jamais recalibré sur les résultats futurs de production)
- model2c/prod_assets/manifest.json          (hashes + hyperparamètres figés)

HYPERPARAMÈTRES : ceux validés en 2C sur les plis de VALIDATION puis FIGÉS —
ils NE SONT PAS re-sélectionnés ici (elo_k=32, dc_xi=0.003, weight_c=0.5).

Prérequis : data/2c_offline/ol_matches.json (python3 scripts/2c_ingest.py --step ol)
Usage     : python3 scripts/2c1_build_assets.py [--prod-db /tmp/prod2c1.sqlite]
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GH_BACKUP", "0")
os.environ["PRONOFOOT_NO_THREADS"] = "1"
os.environ.setdefault("PRONOFOOT_DB", os.path.join("data", "2c_offline", "2c.db"))

from model2c import dc as dcm                              # noqa: E402,F401
from model2c.calibration import MulticlassCalibrator      # noqa: E402
from model2c.identity import canonical_team               # noqa: E402
from model2c.pipeline import run_pass                      # noqa: E402
from model2c.registry2c import compute_code_hash, compute_dataset_hash  # noqa: E402
from model2c.walkforward import by_season_folds            # noqa: E402

# ---- hyperparamètres FIGÉS (sélectionnés sur validation 2C — rapport 2C §11)
FROZEN = {"elo_k": 32, "dc_xi": 0.003, "weight_c": 0.5}
CALIBRATION_VERSION = "2c-calib-prod-1.0"
ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "model2c", "prod_assets")


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod-db", default="/tmp/prod2c1.sqlite",
                    help="copie du backup prod (contrôle de couverture identité)")
    args = ap.parse_args()

    src = os.path.join("data", "2c_offline", "ol_matches.json")
    payload = json.load(open(src))
    matches = payload["matches"]
    assert len(matches) == 6120, f"dataset inattendu : {len(matches)}"
    print(f"[assets] dataset réel : {len(matches)} matchs "
          f"({payload['generated_at']})")

    folds = by_season_folds(matches)
    print(f"[assets] passe unique (hyperparamètres figés {FROZEN}) sur "
          f"{len(folds)} plis…")
    records = run_pass(matches, folds, elo_k=FROZEN["elo_k"],
                       dc_xi=FROZEN["dc_xi"], weight_c=FROZEN["weight_c"],
                       fit_dc=True)
    ok = [r for r in records if r.get("prediction_status") == "OK"]
    print(f"[assets] enregistrements évaluables : {len(ok)}")

    # ---- calibration FIGÉE (Platt, tout l'historique réel évaluable) -------
    calib = {"calibration_version": CALIBRATION_VERSION, "method": "platt",
             "trained_on": "openligadb bl1+bl2 — saisons 2019→2025 (réel)",
             "models": {}}
    for key, model_id in (("B", "MODEL_2C_B"), ("C", "MODEL_2C_C"),
                          ("D", "MODEL_2C_D")):
        pts = [(r[key]["dist"], r["outcome"]) for r in ok if key in r]
        cal = MulticlassCalibrator("platt").fit([p[0] for p in pts],
                                                [p[1] for p in pts])
        calib["models"][model_id] = {
            "n": len(pts), "trained_classes": cal.trained_classes,
            "classes": {c: {"a": round(k.a, 8), "b": round(k.b, 8),
                            "n": k.n, "trained": k.trained}
                        for c, k in cal.per_class.items()}}
        print(f"[assets] calibration {model_id}: n={len(pts)} "
              f"classes_entraînées={cal.trained_classes}")

    # ---- seed compact -------------------------------------------------------
    seed = {"version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "openligadb via WEB-4 SafeHttpClient (réel)",
            "matches": [[m["league"], m["season"], m["kickoff_utc"],
                         m["home"], m["away"], m["hg"], m["ag"]]
                        for m in matches]}

    os.makedirs(ASSETS_DIR, exist_ok=True)
    p_seed = os.path.join(ASSETS_DIR, "seed_bl_history.json")
    p_cal = os.path.join(ASSETS_DIR, "calibration_2c_v1.json")
    json.dump(seed, open(p_seed, "w"), ensure_ascii=False, separators=(",", ":"))
    json.dump(calib, open(p_cal, "w"), ensure_ascii=False, indent=1)

    manifest = {
        "generated_at": seed["generated_at"],
        "dataset_hash": compute_dataset_hash(matches),
        "code_hash": compute_code_hash(),
        "seed_sha256": _sha_file(p_seed),
        "calibration_sha256": _sha_file(p_cal),
        "seed_matches": len(matches),
        "frozen_hyperparams": FROZEN,
        "calibration_version": CALIBRATION_VERSION,
        "note": ("Assets FIGÉS pour le shadow 2C.1. La calibration ne sera "
                 "JAMAIS ré-entraînée sur la production (§10) : toute "
                 "nouvelle calibration = nouvelle version, décision humaine."),
    }

    # ---- contrôle de couverture identité sur la COPIE du backup prod -------
    coverage = {"checked": 0, "mapped": 0, "unknown": []}
    if os.path.exists(args.prod_db):
        import sqlite3
        con = sqlite3.connect(args.prod_db)
        names = {r[0] for r in con.execute(
            "SELECT home_team FROM matches WHERE competition IN ('ger.1','ger.2')"
        )} | {r[0] for r in con.execute(
            "SELECT away_team FROM matches WHERE competition IN ('ger.1','ger.2')")}
        con.close()
        seed_teams = {m[3] for m in seed["matches"]} | {m[4] for m in seed["matches"]}
        for n in sorted(names):
            coverage["checked"] += 1
            c = canonical_team(n)
            if c and c in seed_teams:
                coverage["mapped"] += 1
            else:
                coverage["unknown"].append(n)
        print(f"[assets] couverture identité prod : {coverage['mapped']}/"
              f"{coverage['checked']} équipes mappées")
        if coverage["unknown"]:
            print(f"[assets] NON MAPPÉES (honnête → IDENTITY_UNKNOWN) : "
                  f"{coverage['unknown']}")
    else:
        print(f"[assets] (backup copie absente : {args.prod_db} — contrôle "
              "identité sauté)")
    manifest["identity_coverage_prod"] = coverage

    p_man = os.path.join(ASSETS_DIR, "manifest.json")
    json.dump(manifest, open(p_man, "w"), ensure_ascii=False, indent=1)
    print(f"[assets] écrits : {p_seed} ({os.path.getsize(p_seed)//1024} Ko), "
          f"{p_cal}, {p_man}")
    print(f"[assets] dataset_hash = {manifest['dataset_hash'][:16]}… "
          f"code_hash = {manifest['code_hash'][:16]}…")


if __name__ == "__main__":
    main()
