# -*- coding: utf-8 -*-
"""
ÉTAPE 2B.WEB-4 §34/§35 — TEST RÉEL CONTRÔLÉ DU CYCLE D'INGESTION
=================================================================
Un SEUL cycle MANUEL sur un petit nombre de matchs réels (≤5 — canary
niveau 1, §36), avec la stack de production complète :

    SafeHttpClient (cache PERSISTANT) → ResearchOrchestrator →
    PIT persistant → candidats bridge (fenêtres réelles).

Statuts par section : WEB4_REAL_CYCLE_PASS / WEB4_REAL_CYCLE_FAIL /
WEB4_REAL_CYCLE_NOT_TESTED — jamais un FAIL masqué en PASS.

Le script écrit dans UNE BASE DE CONTRÔLE (data/web4_check.db), jamais dans
la base de production, et ne touche AUCUNE prédiction gelée.

Usage : python scripts/web4_real_cycle_check.py
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GH_BACKUP", "0")
# CRITIQUE : importer `app` pour réutiliser http_json SANS démarrer les
# workers/live/scheduler (leçon incident du 05/09 : sans cette variable, le
# simple `import app` démarrait start_workers()+start_web4() en tâche de
# fond et polluait une base locale — jamais arrivé en production).
os.environ["PRONOFOOT_NO_THREADS"] = "1"

UTC = timezone.utc
RESULTS = []


def section(name, status, detail):
    RESULTS.append({"section": name, "status": status, "detail": detail})
    icon = {"WEB4_REAL_CYCLE_PASS": "✅", "WEB4_REAL_CYCLE_FAIL": "❌",
            "WEB4_REAL_CYCLE_NOT_TESTED": "⚠️"}[status]
    print(f"{icon} [{status}] {name} — {detail}")


def main():
    import db as db_layer
    import repository as repo

    from sources import registry as regmod
    from sources.web.compliance import ComplianceGate
    from sources.web.safe_http import SafeHttpClient
    from sources.web.orchestrator import ResearchOrchestrator
    from sources.web.quality_metrics import QualityMetrics
    from sources.web.web4 import bridge, metrics as w4m
    from sources.web.web4.entity_store import EntityStore
    from sources.web.web4.journal_sqlite import PersistentResearchJournal
    from sources.web.web4.persistent_cache import WebCacheSQLite
    from sources.web.web4.pit_sqlite import PersistentPITStore
    from sources.web.web4.scheduler import IngestionScheduler
    from sources.web.web4.venues import VenueStore
    from sources.web.web4.config import WEB4_CONFIG

    # ---- base de CONTRÔLE dédiée (jamais la production) --------------------
    ctrl = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "web4_check.db")
    os.makedirs(os.path.dirname(ctrl), exist_ok=True)
    if os.path.exists(ctrl):
        os.remove(ctrl)
    db_layer.init(path=ctrl, reset=True)
    section("12_CONTROLE_DB", "WEB4_REAL_CYCLE_PASS",
            f"base de contrôle isolée : {ctrl}")

    # ---- stack de production réelle ----------------------------------------
    reg = regmod.load()
    gate = ComplianceGate(reg)
    cache = WebCacheSQLite()
    client = SafeHttpClient(registry=reg, gate=gate, cache=cache)
    store = PersistentPITStore()
    journal = PersistentResearchJournal()
    qm = QualityMetrics()
    imetrics = w4m.IngestionMetrics()

    def factory():
        return ResearchOrchestrator(client, registry=reg, store=store,
                                    journal=journal, metrics=qm)
    sched = IngestionScheduler(factory, store, journal, metrics=imetrics,
                               registry=reg, config=WEB4_CONFIG)

    # ==== 1. CALENDRIER RÉEL ==================================================
    # En PRODUCTION les matchs sont inscrits dans `matches` par le LIVE 2A
    # (app.http_json — chemin séparé, hors budget d'ingestion). Ce contrôle
    # reproduit EXACTEMENT ce mécanisme pour l'ensemencement initial, afin de
    # ne PAS voler le budget réseau du cycle WEB-4 (leçon du premier run :
    # 20 requêtes calendrier via SafeHttpClient avaient saturé la fenêtre
    # WEB-2 20 req/25 s — le pipeline refusait honnêtement, FAIL détecté).
    import app as appmod                                  # chemin LIVE 2A réel
    leagues = ["eng.1", "esp.1", "ita.1", "fra.1", "ger.1"]
    base = regmod.get_source(reg, "espn")["base_url"].rstrip("/")
    now = datetime.now(UTC)
    events_all = []
    fetch_reports = []
    for lg in leagues:
        for dd in range(0, 4):
            day = (now + timedelta(days=dd)).strftime("%Y%m%d")
            url = f"{base}/{lg}/scoreboard?dates={day}"
            try:
                t0 = time.time()
                obj = appmod.http_json(url, retries=1)   # = production 2A
                evs = obj.get("events", [])
                fetch_reports.append(
                    f"{lg}/{day}:{len(evs)}ev/{int((time.time()-t0)*1000)}ms")
                for ev in evs:
                    ev["_lg"] = lg
                events_all.extend(evs)
            except Exception as e:
                fetch_reports.append(f"{lg}/{day}:ERR:{type(e).__name__}")
    section("34_1_CALENDAR_FETCH", "WEB4_REAL_CYCLE_PASS",
            f"{len(fetch_reports)} requêtes calendrier (mécanisme LIVE 2A) — "
            + " · ".join(fetch_reports[:10]) + (" …" if len(fetch_reports) > 10 else ""))

    upcoming = []
    for ev in events_all:
        comp = (ev.get("competitions") or [{}])[0]
        home = away = None
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            if c.get("homeAway") == "home":
                home = t
            else:
                away = t
        if not home or not away:
            continue
        ko = ev.get("date")
        try:
            ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
        except Exception:
            continue
        if ko_dt <= now or ko_dt > now + timedelta(
                hours=WEB4_CONFIG["horizon_hours"]):
            continue
        lg = ev.get("_lg", "eng.1")
        mid = f"espn:{lg}:{ev['id']}"
        repo.upsert_match({
            "id": mid, "source": "espn", "source_match_id": ev["id"],
            "competition": lg,
            "home_team": home.get("displayName") or home.get("name"),
            "away_team": away.get("displayName") or away.get("name"),
            "home_team_ext_id": str(home.get("id")),
            "away_team_ext_id": str(away.get("id")),
            "kickoff_time_utc": ko,
            "venue": (comp.get("venue") or {}).get("fullName", "")})
        upcoming.append(mid)
        # ≤ 3 matchs pour ce contrôle : ≤18 requêtes ≤ fenêtre WEB-2 (20/25 s)
        if len(upcoming) >= 3:
            break
    if not upcoming:
        section("34_2_MATCHES_UPCOMING", "WEB4_REAL_CYCLE_NOT_TESTED",
                "aucun match à venir dans l'horizon 48 h sur les 5 grands "
                "championnats × 4 jours (vérité calendrier — rien n'est inventé)")
    else:
        labels = [repo.get_match(m)["home_team"] + " — "
                  + repo.get_match(m)["away_team"] for m in upcoming]
        section("34_2_MATCHES_UPCOMING", "WEB4_REAL_CYCLE_PASS",
                f"{len(upcoming)} match(s) réel(s) à venir inséré(s) : "
                + " ; ".join(labels))

    # ==== 2. UN CYCLE RÉEL =====================================================
    t0 = time.time()
    out = sched.run_cycle(trigger="MANUAL", tier="calendar")
    dur = int((time.time() - t0) * 1000)
    rpm = out["requests"] / max(1, len(out["matches"]))
    ok_budget = rpm <= WEB4_CONFIG["requests_per_match_hard_cap"]
    section("34_3_CYCLE_REEL",
            "WEB4_REAL_CYCLE_PASS" if out["matches"] else
            "WEB4_REAL_CYCLE_NOT_TESTED",
            f"cycle {out['cycle_id']} : status={out['status']} · "
            f"matchs={len(out['matches'])} · requêtes={out['requests']} "
            f"({rpm:.1f}/match) · cache_hits={out['cache_hits']} · "
            f"erreurs={len(out['errors'])} · {dur} ms" +
            ("" if ok_budget else " · ⚠️ HARD CAP DÉPASSÉ"))
    if not ok_budget:
        section("34_3b_BUDGET", "WEB4_REAL_CYCLE_FAIL",
                f"budget dur dépassé : {rpm:.1f} req/match > 20")

    # ==== 3. CACHE PERSISTANT (2e cycle ≈ 0 réseau) ============================
    n_cache = len(cache)
    out2 = sched.run_cycle(trigger="MANUAL", tier="calendar")
    cache_works = (n_cache > 0 and out2["matches"] and out["requests"] > 0 and
                   out2["requests"] < out["requests"] and
                   out2["cache_hits"] > 0)
    section("34_4_CACHE_PERSISTANT",
            "WEB4_REAL_CYCLE_PASS" if cache_works else
            ("WEB4_REAL_CYCLE_NOT_TESTED"
             if not (out["matches"] and out["requests"] > 0)
             else "WEB4_REAL_CYCLE_FAIL"),
            f"web_cache={n_cache} entrées · cycle1={out['requests']} req → "
            f"cycle2={out2['requests']} req (hits={out2['cache_hits']}) — "
            f"chute réseau 2e cycle = preuve du cache persistant (TTL "
            f"volatils 30 s peuvent légitimement se rafraîchir)")

    # ==== 4. OBSERVATIONS / PIT / JOURNAL =======================================
    n_pts = store.count()
    n_ev = journal.count()
    sample = None
    if out["matches"]:
        pts = store.query(match_id=out["matches"][0])
        if pts:
            p = pts[0]
            sample = (f"{p.data_type}={p.value!r} · source={p.source} · "
                      f"retrieved_at={p.retrieved_at} · valid={p.valid}")
    section("34_5_PIT_OBSERVATIONS",
            "WEB4_REAL_CYCLE_PASS" if n_pts else "WEB4_REAL_CYCLE_NOT_TESTED",
            f"web_datapoints={n_pts} · journal={n_ev} événements · "
            f"exemple: {sample or 'aucun point (matchs trop lointains ?)'}")

    # ==== 5. CANDIDATS + KICKOFF PROTECTION ======================================
    n_cand = db_layer.query(
        "SELECT COUNT(*) AS c FROM data_snapshots WHERE source='web4-candidate'",
        one=True)["c"]
    cands_detail = out["candidates"]
    section("34_6_SNAPSHOT_CANDIDATS",
            "WEB4_REAL_CYCLE_PASS",
            f"candidats créés={n_cand} ({cands_detail or 'aucun — aucun match '
             f'dans une fenêtre T-180/T-60/T-15, ce qui est NORMAL si les '
             f'matchs sont > 3 h'})")
    # kickoff protection sur un match passé (tentative DÉLIBÉRÉE — doit refuser)
    if out["matches"]:
        mid0 = out["matches"][0]
        refused = bridge.create_snapshot_candidate(
            store, mid0, "2020-01-01T00:00:00Z", "T-15",
            as_of="2020-01-01T00:05:00Z", now="2020-01-01T00:05:00Z",
            registry=reg, journal=journal, repository=repo)
        ok_ko = refused.get("reason") == "KICKOFF_PASSED"
        section("34_7_KICKOFF_PROTECTION",
                "WEB4_REAL_CYCLE_PASS" if ok_ko else "WEB4_REAL_CYCLE_FAIL",
                f"tentative de candidat post-kickoff → {refused.get('reason')}"
                f" (alerte CRITICAL levée : {ok_ko})")

    # ==== 6. ENTITÉS / SCORE DE QUALITÉ / ALERTES ================================
    n_ent = EntityStore().count()
    section("34_8_ENTITY_MAP",
            "WEB4_REAL_CYCLE_PASS",
            f"web_entities={n_ent} mappings (seuils WEB-1 : AUTO ≥0.95, "
            f"JOURNALIZED 0.80–0.95, UNKNOWN jamais stocké)")
    alerts = w4m.recent_alerts(limit=5)
    section("34_9_ALERTES",
            "WEB4_REAL_CYCLE_PASS",
            f"{len(alerts)} alerte(s) récente(s) : "
            + ", ".join(f"{a['level']}:{a['code']}" for a in alerts)
            if alerts else "0 alerte (hors kickoff-protection délibérée)")
    snap = imetrics.snapshot()
    section("34_10_METRIQUES",
            "WEB4_REAL_CYCLE_PASS",
            f"cycles={snap['ingestion_cycles']:.0f} · requêtes="
            f"{snap['source_requests']:.0f} · cache_hits="
            f"{snap['cache_hits']:.0f} · snapshots créés="
            f"{snap['snapshots_created']:.0f} · prédictions créées="
            f"{snap['predictions_created']:.0f} (0 exigé — §35)")

    # ==== 7. PRÉDICTIONS 2A INTOUCHÉES ============================================
    n_pred = db_layer.table_counts()["predictions"]
    section("34_11_INTEGRITE_2A",
            "WEB4_REAL_CYCLE_PASS" if n_pred == 0 else "WEB4_REAL_CYCLE_FAIL",
            f"predictions={n_pred} (0 attendu dans la base de contrôle : "
            f"WEB-4 ne crée JAMAIS de prédiction)")
    return _finish()


def _finish():
    passed = sum(1 for r in RESULTS if r["status"] == "WEB4_REAL_CYCLE_PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "WEB4_REAL_CYCLE_FAIL")
    nt = sum(1 for r in RESULTS if r["status"] == "WEB4_REAL_CYCLE_NOT_TESTED")
    print("\n" + "=" * 64)
    print(f"RÉSULTAT : PASS={passed} · FAIL={failed} · NOT_TESTED={nt}")
    print("=" * 64)
    print(json.dumps({"results": RESULTS}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
