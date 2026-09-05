# -*- coding: utf-8 -*-
"""
ÉTAPE 2C — EXÉCUTION DU BACKTEST WALK-FORWARD COMPLET
======================================================
Données : data/2c_offline/ol_matches.json (6 120 matchs réels OpenLigaDB)
          data/2c_offline/sb_xg_map.json (xG RÉEL StatsBomb, couverture partielle)
Sorties : data/2c_offline/report_2c.json
          persistance shadow/registry/features dans PRONOFOOT_DB dédiée
          (data/2c_offline/2c.db — JAMAIS la base de production).

Usage : python scripts/2c_run_backtest.py [--no-persist]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GH_BACKUP", "0")
os.environ["PRONOFOOT_NO_THREADS"] = "1"
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "2c_offline", "2c.db")
os.environ.setdefault("PRONOFOOT_DB", _DEFAULT_DB)

import db as db_layer                                    # noqa: E402
from model2c.pipeline import full_backtest              # noqa: E402
from model2c.config2c import CONFIG_2C as C             # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args()

    outdir = C["offline_dir"]
    with open(os.path.join(outdir, "ol_matches.json"), encoding="utf-8") as f:
        ol = json.load(f)
    with open(os.path.join(outdir, "sb_xg_map.json"), encoding="utf-8") as f:
        sb = json.load(f)
    matches = ol["matches"]
    print(f"[2c] dataset : {len(matches)} matchs réels "
          f"({ol['leagues']} {ol['seasons'][0]}→{ol['seasons'][-1]})")
    print(f"[2c] xG réel : {sb['meta']['with_xg']}/{sb['meta']['covered_matches']} matchs couverts")

    if not args.no_persist:
        db_layer.init(os.environ["PRONOFOOT_DB"])

    t0 = datetime.now(timezone.utc)
    report = full_backtest(matches, xg_map=sb["xg_map"], persist=not args.no_persist,
                           progress=lambda m: print(m, flush=True))
    dt = (datetime.now(timezone.utc) - t0).total_seconds()

    path = os.path.join(outdir, "report_2c.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"[2c] backtest terminé en {dt:.0f}s → {path}")

    # ---- résumé console ----
    hp = report["hyperparams"]
    print(f"[2c] hyperparamètres (validation) : elo_k={hp['elo_k']} dc_xi={hp['dc_xi']} "
          f"weight_c={hp['weight_c']} D_justified={hp['ensemble_D_justified']}")
    for key in ("B", "C", "D"):
        cmp_ = report["comparison_2a_final"].get(key)
        if cmp_:
            print(f"[2c] 2A vs {key} : {cmp_['verdict']} (n={cmp_['n']})")


if __name__ == "__main__":
    main()
