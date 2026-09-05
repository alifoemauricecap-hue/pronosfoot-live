# -*- coding: utf-8 -*-
"""
ÉTAPE 2B.WEB-3 §29 — TESTS RÉSEAU CONTRÔLÉS (intégration réelle)
================================================================
Exécution SÉPARÉE des tests unitaires — ce script touche RÉELLEMENT
Internet, via le SEUL chemin autorisé : SafeHttpClient + ComplianceGate
(WEB-2), sur le VRAI registry (aucun override, aucune relaxation).

Règles :
- UNIQUEMENT les 6 sources autorisées (§2) ;
- 1 requête par source (politesse quota ; 6 requêtes au total) ;
- compliance ALLOW obligatoire — un DENY est signalé, JAMAIS contourné ;
- le résultat réel est rapporté tel quel : REAL_FETCH_PASS /
  REAL_FETCH_FAIL / NOT_TESTED — JAMAIS de FAIL transformé en PASS ;
- aucun secret, aucune donnée utilisée en production.

Usage : python3 scripts/real_fetch_check.py [--json]
"""
import json
import sys
import time

sys.path.insert(0, ".")

from sources import registry as regmod
from sources.web.compliance import ComplianceGate
from sources.web.safe_http import SafeHttpClient
from sources.web.extractors.base import ensure_obj, FetchContext
from sources.web.extractors import espn as XE, football_data as XF, \
    open_meteo as XO, openligadb as XL, statsbomb as XS, wikidata as XW

UA_CHECK = []


def _summary_espn(body):
    obj = ensure_obj(body)
    events = obj.get("events") or []
    leagues = [l.get("slug") for l in (obj.get("leagues") or [])]
    return {"events": len(events), "leagues": leagues[:3]}


def _summary_fdcsv(body):
    text = body.decode("latin-1", errors="replace")
    ctx = FetchContext(retrieved_at=None, season="2526")
    rows, _ = XF.parse_csv(text, ctx)
    odds_n = sum(len(r["odds"]) for r in rows)
    return {"matches": len(rows), "odds_cells": odds_n,
            "first": f"{rows[0]['home']} {rows[0]['fthg']}-{rows[0]['ftag']} "
                     f"{rows[0]['away']}" if rows else None}


def _summary_om(body):
    obj = ensure_obj(body)
    keys = sorted(set((obj.get("current") or {}).keys())
                  | set((obj.get("hourly") or {}).keys()) - {"time"})
    return {"weather_keys": keys[:8], "lat": obj.get("latitude"),
            "lon": obj.get("longitude")}


def _summary_ol(body):
    obj = ensure_obj(body)
    return {"matches": len(obj) if isinstance(obj, list) else 0}


def _summary_sb(body):
    obj = ensure_obj(body)
    names = [c.get("competition_name") for c in
             (obj if isinstance(obj, list) else [])]
    return {"competitions": len(names), "sample": names[:3]}


def _summary_wd(body):
    obj = ensure_obj(body)
    res = (obj.get("search") or [])
    return {"candidates": len(res),
            "first": f"{res[0].get('id')} {res[0].get('label')}"
            if res else None}


REQUESTS = [
    {"source": "espn",
     "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
     "data_type": "score_live", "summarize": _summary_espn},
    {"source": "football_data_co_uk",
     "url": "https://www.football-data.co.uk/mmz4281/2526/E0.csv",
     "data_type": "historical_results", "summarize": _summary_fdcsv},
    {"source": "open_meteo",
     "url": ("https://api.open-meteo.com/v1/forecast?latitude=54.9756"
             "&longitude=-1.6217&current=temperature_2m,precipitation,"
             "relative_humidity_2m,wind_speed_10m,weather_code"),
     "data_type": "weather", "summarize": _summary_om},
    {"source": "openligadb",
     "url": "https://api.openligadb.de/getmatchdata/bl1/2025",
     "data_type": "match_calendar", "summarize": _summary_ol},
    {"source": "statsbomb_open",
     "url": ("https://raw.githubusercontent.com/statsbomb/open-data/master"
             "/data/competitions.json"),
     "data_type": "historical_results", "summarize": _summary_sb},
    {"source": "wikidata",
     "url": ("https://www.wikidata.org/w/api.php?action=wbsearchentities"
             "&search=Paris%20Saint-Germain&language=fr&format=json"),
     "data_type": "entity_identity", "summarize": _summary_wd},
]


def main():
    reg = regmod.load()
    gate = ComplianceGate(reg)                 # AUCUN override
    logs = []
    client = SafeHttpClient(registry=reg, gate=gate,
                            config={"retry": {"delay_sec": 1.0}},
                            logger=logs.append)
    results = []
    n_pass = n_fail = n_not = 0
    for rq in REQUESTS:
        sid = rq["source"]
        decision = gate.check_source_access(sid, rq["url"], rq["data_type"])
        if decision.decision != "ALLOW":
            n_not += 1
            results.append({"source": sid, "status": "NOT_TESTED",
                            "reason": f"COMPLIANCE_{decision.decision}",
                            "details": list(decision.reasons)})
            continue
        t0 = time.time()
        try:
            resp = client.fetch(sid, rq["url"], rq["data_type"],
                                allow_cache=False)
        except Exception as e:                   # rapporté tel quel
            n_fail += 1
            results.append({"source": sid, "status": "REAL_FETCH_FAIL",
                            "error": getattr(e, "code",
                                             type(e).__name__)})
            continue
        dt = time.time() - t0
        try:
            summary = rq["summarize"](resp.body)
        except Exception as e:
            n_fail += 1
            results.append({"source": sid, "status": "REAL_FETCH_FAIL",
                            "error": f"PARSE_{type(e).__name__}",
                            "http": resp.status_code,
                            "latency_ms": resp.latency_ms})
            continue
        n_pass += 1
        results.append({"source": sid, "status": "REAL_FETCH_PASS",
                        "http": resp.status_code,
                        "latency_ms": round(resp.latency_ms or dt * 1000, 1),
                        "bytes": resp.content_length,
                        "retrieved_at": resp.retrieved_at,
                        "summary": summary})
        time.sleep(1.0)                          # politesse inter-sources

    print("=" * 66)
    print("TESTS RÉSEAU CONTRÔLÉS — 2B.WEB-3 §29 (exécution réelle)")
    print("=" * 66)
    for r in results:
        line = (f"{r['source']:22s} {r['status']}")
        if r["status"] == "REAL_FETCH_PASS":
            line += (f"  http={r['http']} {r['latency_ms']}ms "
                     f"{r['bytes']}o  {r['summary']}")
        elif r["status"] == "REAL_FETCH_FAIL":
            line += f"  error={r.get('error')}"
        else:
            line += f"  {r.get('reason')} {r.get('details')}"
        print(line)
    print("-" * 66)
    print(f"REAL_FETCH_PASS={n_pass}  REAL_FETCH_FAIL={n_fail}  "
          f"NOT_TESTED={n_not}")
    print(f"(secrets dans les logs : "
          f"{any('ghp_' in json.dumps(l) for l in logs)})")
    if "--json" in sys.argv:
        print(json.dumps({"results": results, "pass": n_pass,
                          "fail": n_fail, "not_tested": n_not,
                          "logs": logs}, ensure_ascii=False, indent=1))
    return 0                                     # statuts dans le rapport


if __name__ == "__main__":
    sys.exit(main())
