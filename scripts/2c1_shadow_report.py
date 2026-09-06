# -*- coding: utf-8 -*-
"""
ÉTAPE 2C.1 — RAPPORT D'ÉTAT SHADOW (lecture seule)
===================================================
Affiche l'état du shadow sur une DB donnée (copie locale ou — plus tard, sur
ordre de Maurice — extraction de la prod). Jamais de probabilités détaillées :
compteurs, refus, timings, comparateur (N et verdict d'échantillon - §24).

Usage : PRONOFOOT_DB=/chemin/db.sqlite python3 scripts/2c1_shadow_report.py
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GH_BACKUP", "0")
os.environ["PRONOFOOT_NO_THREADS"] = "1"

import db                                                      # noqa: E402
from model2c import shadow_monitor                              # noqa: E402
from model2c.shadow_prod import ShadowRunner                    # noqa: E402


def main():
    out = {"monitor": shadow_monitor.status_block(db)}
    try:
        hb = db.rows_to_dicts(db.query(
            """SELECT cycle_id, at_utc, duration_ms, timings_json, summary_json
               FROM model2c_shadow_heartbeats ORDER BY id DESC LIMIT 5"""))
        out["last_heartbeats"] = [
            {"cycle_id": h["cycle_id"], "at": h["at_utc"],
             "duration_ms": h["duration_ms"], "timings": h["timings_json"]}
            for h in hb]
        out["refusals"] = dict(Counter(
            r["refusal_reason"] for r in db.rows_to_dicts(db.query(
                "SELECT refusal_reason FROM predictions_2c_shadow"
                " WHERE status='NO_PREDICTION'"))))
        out["labels_ok"] = dict(Counter(
            r["snapshot_label"] for r in db.rows_to_dicts(db.query(
                "SELECT snapshot_label FROM predictions_2c_shadow"
                " WHERE status='OK'"))))
        out["alerts_recent"] = db.rows_to_dicts(db.query(
            "SELECT level, code, detail_json, at_utc FROM model2c_shadow_alerts"
            " ORDER BY id DESC LIMIT 5"))
        out["team_events"] = db.query(
            "SELECT COUNT(*) c FROM model2c_team_events", one=True)["c"]
        out["dc_params_cached"] = db.rows_to_dicts(db.query(
            "SELECT league, fit_day, n_train FROM model2c_dc_params"))
        out["comparator"] = ShadowRunner(db_module=db).compare_with_2a()
    except Exception as e:
        out["read_error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
