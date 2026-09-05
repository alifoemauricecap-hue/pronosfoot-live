# -*- coding: utf-8 -*-
"""
ÉTAPE 2C — INGESTION HISTORIQUE RÉELLE (offline, une seule fois)
==================================================================
Sources UNIQUEMENT via SafeHttpClient + ComplianceGate (architecture WEB-4) :

- OpenLigaDB  : bl1+bl2, saisons 2016→2025 (1 requête/ligue-saison ≈ 20 req)
- StatsBomb   : 1. Bundesliga 2023/24 (comp 9 / saison 281) — événements, xG
                RÉEL. Couverture PARTIELLE assumée (34 matchs réellement
                publiés par StatsBomb — jamais extrapolée).
- football-data.co.uk : NON utilisé — panne HTTP 503 constatée toute la
  journée (audit 2C section C). Aucune donnée simulée à la place.

Sorties (artifacts hors-ligne, gitignored) :
- data/2c_offline/ol_matches.json   (matchs canoniques, horloges PIT)
- data/2c_offline/sb_xg_map.json    (xG réel par match couvert) + meta

Échecs = comptés et rapportés, jamais masqués. Usage :
    python scripts/2c_ingest.py --step ol|sb|all
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GH_BACKUP", "0")
os.environ["PRONOFOOT_NO_THREADS"] = "1"

from sources import registry as regmod                      # noqa: E402
from sources.web.compliance import ComplianceGate            # noqa: E402
from sources.web.safe_http import SafeHttpClient             # noqa: E402

from model2c.config2c import CONFIG_2C as C                  # noqa: E402
from model2c import ingest_history as ing                    # noqa: E402

OUTDIR = C["offline_dir"]


def _client():
    reg = regmod.load()
    gate = ComplianceGate(reg)            # aucun override, aucun contournement
    return SafeHttpClient(registry=reg, gate=gate)


def step_ol(client):
    os.makedirs(OUTDIR, exist_ok=True)
    all_matches, failures = [], []
    for league in C["ol_leagues"]:
        for season in C["ol_seasons"]:
            try:
                ms = ing.fetch_ol_season(client, league, season)
                all_matches.extend(ms)
                print(f"  ✅ OL {league}/{season} : {len(ms)} matchs réels")
            except Exception as e:
                failures.append({"league": league, "season": season,
                                 "error": type(e).__name__, "detail": str(e)[:160]})
                print(f"  ❌ OL {league}/{season} : {type(e).__name__} {str(e)[:120]}")
            time.sleep(1.5)  # politesse + fenêtre SafeHttp (jamais de burst)
    all_matches.sort(key=lambda m: m["kickoff_utc"])
    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "source": "openligadb", "leagues": C["ol_leagues"],
           "seasons": C["ol_seasons"], "match_count": len(all_matches),
           "failures": failures, "matches": all_matches}
    path = os.path.join(OUTDIR, "ol_matches.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"→ {path} : {len(all_matches)} matchs | échecs: {len(failures)}")
    return all_matches, failures


def step_sb(client):
    os.makedirs(OUTDIR, exist_ok=True)
    comp, season = C["sb_xg_competition_id"], C["sb_xg_season_id"]
    xg_map, meta = ing.build_sb_xg_map(client, comp, season, OUTDIR, sleep_s=1.3)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "competition_id": comp, "season_id": season,
           "meta": meta, "xg_map": xg_map}
    path = os.path.join(OUTDIR, "sb_xg_map.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"→ {path} : {meta}")
    return xg_map, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", choices=["ol", "sb", "all"], default="all")
    args = ap.parse_args()
    client = _client()
    if args.step in ("ol", "all"):
        step_ol(client)
    if args.step in ("sb", "all"):
        step_sb(client)


if __name__ == "__main__":
    main()
