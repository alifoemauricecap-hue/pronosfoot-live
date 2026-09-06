# -*- coding: utf-8 -*-
"""
ÉTAPE 2C.1 §21 — CANARY LOCAL SUR **COPIE** DU BACKUP PRODUCTION
=================================================================
Jamais sur la base de production. Séquence PROUVÉE en sortie JSON :

1. copie du backup → fichier de travail temporaire ;
2. migration v3+v4 appliquée À LA COPIE (additif) ;
3. hash SHA-256 de toutes les tables 2A AVANT ;
4. cycles shadow SIMULÉS (SHADOW ON) à des « maintenant » réels du 13/09/2026
   (matchs ger.1/ger.2 réels UPCOMING du backup) — AUCUNE requête réseau ;
5. hash 2A APRÈS → DOIVENT être identiques (non-régression §20) ;
6. règlement artificiel (comme 2A le ferait) d'UN match prédit → la prédiction
   shadow DOIT rester inchangée (anti-fuite §21) ;
7. rapport JSON dans /home/user/2C1_LOCAL_CANARY.json.

Usage : python3 scripts/2c1_local_canary.py [backup.sqlite]
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GH_BACKUP", "0")
os.environ["PRONOFOOT_NO_THREADS"] = "1"
os.environ["PRONOFOOT_2C_SHADOW"] = "1"          # canary : activation explicite

import db                                          # noqa: E402

BACKUP = sys.argv[1] if len(sys.argv) > 1 else "/tmp/prod2c1.sqlite"
TABLES_2A = ("matches", "results", "data_snapshots", "model_versions",
             "predictions", "prediction_results", "odds", "prediction_events",
             "cache_store", "web_cache", "web_datapoints", "web_research_events",
             "web_ingestion_cycles", "web_entities", "web_venues", "web_metrics",
             "web_scheduler_state", "web_alerts")


def table_hashes():
    out = {}
    for t in TABLES_2A:
        payload = json.dumps(db.rows_to_dicts(db.query(f"SELECT * FROM {t}")),
                             sort_keys=True, default=str)
        out[t] = hashlib.sha256(payload.encode()).hexdigest()
    return out


def main():
    assert os.path.exists(BACKUP), f"backup introuvable : {BACKUP}"
    workdir = tempfile.mkdtemp(prefix="canary2c1_")
    copy_path = os.path.join(workdir, "copy.sqlite")
    shutil.copyfile(BACKUP, copy_path)
    report = {"backup_source": BACKUP, "working_copy": copy_path,
              "steps": [], "started_at": datetime.now(timezone.utc).isoformat()}

    work = os.path.join(workdir, "work.db")
    shutil.copyfile(copy_path, work)
    db.init(work, reset=True)
    report["steps"].append({"step": "copy+migrate",
                            "migration_after": db.migration_version()})

    hashes_before = table_hashes()
    report["hash_2a_before"] = hashes_before

    # ---- cycles simulés aux « maintenant » réels du 13/09/2026 -------------
    from model2c.shadow_prod import ShadowRunner
    sims = ["2026-09-13T10:00:00+00:00", "2026-09-13T12:50:00+00:00",
            "2026-09-13T14:40:00+00:00", "2026-09-13T15:20:00+00:00"]
    cycles = []
    for i, sim in enumerate(sims):
        now = datetime.fromisoformat(sim)
        s = ShadowRunner(db_module=db, store=None, max_matches=5).run_once(
            now=now, cycle_id=f"canary-{i}")
        cycles.append({k: s[k] for k in ("at", "cycle_id", "eligible",
                                         "predictions", "skipped_dedup",
                                         "refusals", "errors", "timings")})
    report["cycles"] = cycles

    hashes_after = table_hashes()
    report["hash_2a_after"] = hashes_after
    diff = {t: (hashes_before[t], hashes_after[t]) for t in TABLES_2A
            if hashes_before[t] != hashes_after[t]}
    report["nonregression_2a"] = {"identical": not diff,
                                  "tables_changed": sorted(diff)}

    # ---- statistiques shadow ----------------------------------------------
    cnt = lambda sql, p=(): db.query(sql, p, one=True)["c"]
    report["shadow_stats"] = {
        "predictions_ok": cnt("SELECT COUNT(*) c FROM predictions_2c_shadow WHERE status='OK'"),
        "refusals": {r["refusal_reason"]: r["n"] for r in db.query(
            "SELECT refusal_reason, COUNT(*) n FROM predictions_2c_shadow"
            " WHERE status='NO_PREDICTION' GROUP BY refusal_reason")},
        "heartbeats": cnt("SELECT COUNT(*) c FROM model2c_shadow_heartbeats"),
        "features_2c": cnt("SELECT COUNT(*) c FROM features_2c"),
        "team_events": cnt("SELECT COUNT(*) c FROM model2c_team_events"),
        "dc_params": cnt("SELECT COUNT(*) c FROM model2c_dc_params"),
        "labels": {r["snapshot_label"]: r["n"] for r in db.query(
            "SELECT snapshot_label, COUNT(*) n FROM predictions_2c_shadow"
            " WHERE status='OK' GROUP BY snapshot_label")},
        "models": {r["model_id"]: r["n"] for r in db.query(
            "SELECT model_id, COUNT(*) n FROM predictions_2c_shadow"
            " WHERE status='OK' GROUP BY model_id")},
        "alerts": cnt("SELECT COUNT(*) c FROM model2c_shadow_alerts"),
    }

    # ---- §21 : règlement artificiel d'UN match prédit → immuabilité --------
    ok_row = db.query(
        """SELECT id, match_id FROM predictions_2c_shadow
           WHERE status='OK' ORDER BY created_at LIMIT 1""", one=True)
    immut = {"checked": False}
    if ok_row:
        mid = ok_row["match_id"]
        before = db.rows_to_dicts(db.query(
            "SELECT * FROM predictions_2c_shadow WHERE match_id=%s", (mid,)))
        settle_t = datetime.now(timezone.utc).isoformat()
        db.execute("UPDATE matches SET status='FINISHED', home_score=2, away_score=1"
                   " WHERE id=%s", (mid,))
        db.execute("""INSERT OR IGNORE INTO results
                   (match_id, home_score, away_score, home_ht_score, away_ht_score,
                    final_status, completed_at, source, captured_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                   (mid, 2, 1, 1, 0, "FT", settle_t, "canary-simule", settle_t))
        after = db.rows_to_dicts(db.query(
            "SELECT * FROM predictions_2c_shadow WHERE match_id=%s", (mid,)))
        # nouveau cycle APRÈS le résultat → il ne doit pas casser l'immuabilité
        ShadowRunner(db_module=db, store=None).run_once(
            now=datetime.fromisoformat("2026-09-13T18:00:00+00:00"),
            cycle_id="canary-post-settlement")
        after2 = db.rows_to_dicts(db.query(
            "SELECT * FROM predictions_2c_shadow WHERE match_id=%s", (mid,)))
        immut = {"checked": True, "match_id": mid,
                 "prediction_unchanged": before == after == after2,
                 "result_now_folded": cnt(
                     "SELECT COUNT(*) c FROM model2c_team_events WHERE match_id=%s",
                     (mid,)) == 1}
    report["settlement_immutability"] = immut

    # ---- comparateur (honnête : N insuffisant attendu) ----------------------
    from model2c.shadow_prod import ShadowRunner as _SR
    report["comparator"] = _SR(db_module=db).compare_with_2a()

    out = "/home/user/2C1_LOCAL_CANARY.json"
    json.dump(report, open(out, "w"), indent=1, ensure_ascii=False, default=str)
    print(json.dumps({k: report[k] for k in
                      ("nonregression_2a", "shadow_stats",
                       "settlement_immutability")},
                     indent=1, ensure_ascii=False, default=str))
    print("cycles:", json.dumps([{k: c[k] for k in ("at", "eligible", "predictions",
                                                    "skipped_dedup", "refusals")}
                                   for c in cycles], ensure_ascii=False))
    print("comparator:", json.dumps(report["comparator"], ensure_ascii=False))
    print(f"→ {out}")


if __name__ == "__main__":
    main()
