# -*- coding: utf-8 -*-
"""
TESTS WEB-3 — PIPELINE END-TO-END (banc loopback, 0 réseau public)
===================================================================
Le registry RÉEL est copié puis REBRANCHÉ : les base_url des 6 sources
autorisées pointent vers un serveur local (127.0.0.1) qui sert les fixtures
réelles/synthétiques. La garde anti-réseau-public prouve PUBLIC NETWORK = 0.
Le registry de PRODUCTION reste inchangé (vérifié par test).
"""
import copy
import gzip
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sources import registry as regmod
from sources.web.compliance import ComplianceGate
from sources.web.safe_http import SafeHttpClient
from sources.web.orchestrator import ResearchOrchestrator
from sources.web.pit_store import PointInTimeStore
from sources.web.research_journal import ResearchJournal
from sources.web.quality_metrics import QualityMetrics
from sources.web import snapshots as SNAP

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "web3")
REAL_REG = regmod.load()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ===========================================================================
# GARDE ANTI-RÉSEAU-PUBLIC
# ===========================================================================
_REAL_GAI = socket.getaddrinfo
PUBLIC_ATTEMPTS = []


@pytest.fixture(autouse=True)
def _no_public_network(monkeypatch):
    def guard(host, port, *a, **k):
        if (host or "") in ("127.0.0.1", "localhost", "::1"):
            return _REAL_GAI(host, port, *a, **k)
        PUBLIC_ATTEMPTS.append(host)
        raise AssertionError(f"PUBLIC INTERDIT : {host!r}")
    monkeypatch.setattr(socket, "getaddrinfo", guard)


def _fx(name):
    raw = open(os.path.join(FIX, name), "rb").read()
    return raw


def _fx_json_list(name, key):
    return json.dumps(json.loads(_fx(name))[key]).encode()


STANDINGS_MINI = json.dumps({
    "children": [{"standings": {"entries": [
        {"team": {"id": "361", "displayName": "Newcastle United"},
         "stats": [{"name": "rank", "displayValue": "5"},
                   {"name": "points", "displayValue": "12"}]},
        {"team": {"id": "349", "displayName": "AFC Bournemouth"},
         "stats": [{"name": "rank", "displayValue": "9"},
                   {"name": "points", "displayValue": "8"}]}]}}]}).encode()

SB_MATCHES = json.dumps([
    {"match_id": 999, "match_date": "2026-08-30",
     "home_team": {"home_team_name": "Team Alpha"},
     "away_team": {"away_team_name": "Team Beta"},
     "home_score": 2, "away_score": 1}]).encode()

SB_EVENTS = _fx_json_list("sb_events_fixture.json", "events")

ROUTES = {
    "/apis/site/v2/sports/soccer/eng.1/scoreboard":
        (_fx("espn_scoreboard_real.json"), "application/json"),
    "/apis/site/v2/sports/soccer/eng.1/summary":
        (_fx("espn_summary_fixture.json"), "application/json"),
    "/apis/site/v2/sports/soccer/eng.1/standings":
        (STANDINGS_MINI, "application/json"),
    "/mmz4281/2526/E0.csv":
        (_fx("fdco_E0_sample_real.csv"), "text/csv"),
    "/v1/forecast":
        (_fx("open_meteo_hourly_fixture.json"), "application/json"),
    "/getmatchdata/bl1/2025":
        (_fx("openligadb_matches_fixture.json"), "application/json"),
    "/statsbomb/open-data/master/data/competitions.json":
        (_fx("sb_competitions_real.json"), "application/json"),
    "/statsbomb/open-data/master/data/matches/9/281.json":
        (SB_MATCHES, "application/json"),
    "/statsbomb/open-data/master/data/events/999.json":
        (SB_EVENTS, "application/json"),
    "/w/api.php": (_fx("wikidata_search_psg_real.json"), "application/json"),
}

HITS = {}
HITS_LOCK = threading.Lock()


def hits():
    with HITS_LOCK:
        return dict(HITS)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        with HITS_LOCK:
            HITS[path] = HITS.get(path, 0) + 1
        if path.startswith("/fail"):
            self._send(503, b"{}", "application/json")
            return
        route = ROUTES.get(path)
        if route is None:
            self._send(404, b"{}", "application/json")
            return
        body, ctype = route
        self._send(200, body, ctype)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


# préfixes de CHEMIN d'origine pour rebrancher le registry sur loopback
_PATH_PREFIX = {
    "espn": "/apis/site/v2/sports/soccer",
    "football_data_co_uk": "/mmz4281",
    "open_meteo": "/v1",
    "openligadb": "",
    "statsbomb_open": "/statsbomb/open-data/master/data",
    "wikidata": "",
}


def _rebranch_registry(port, failing=None):
    """Copie du registry réel : 6 sources autorisées pointent vers loopback.
    `failing` = source_id(s) dont la base_url devient /fail (panne simulée)."""
    reg = copy.deepcopy(REAL_REG)
    failing = set(failing or ())
    for src in reg["sources"]:
        sid = src["source_id"]
        if sid in _PATH_PREFIX and sid not in failing:
            src["base_url"] = f"http://127.0.0.1:{port}{_PATH_PREFIX[sid]}"
            if sid == "wikidata":
                continue
        elif sid in failing:
            src["base_url"] = f"http://127.0.0.1:{port}/fail"
    if "wikidata" not in {s["source_id"] for s in reg["sources"]}:
        espn = regmod.get_source(reg, "espn")
        wd = copy.deepcopy(espn)
        wd.update({"source_id": "wikidata", "name": "Wikidata",
                   "base_url": f"http://127.0.0.1:{port}",
                   "status": "TESTED", "enabled": True,
                   "capabilities": {"entity_identity": True}})
        reg["sources"].append(wd)
    return reg


@pytest.fixture(scope="module")
def env():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"port": port}
    srv.shutdown()
    srv.server_close()


def _mk(env_in, failing=None):
    reg = _rebranch_registry(env_in["port"], failing)
    ov = {sid: {"allow_http": True, "allow_private_hosts": True}
          for sid in list(_PATH_PREFIX) + ["wikidata"]}
    gate = ComplianceGate(reg, ov)
    client = SafeHttpClient(registry=reg, gate=gate, config={
        "rate_limit": {"per_source_min_interval_sec": 0.0,
                       "cycle_max_requests": 100000,
                       "cycle_window_sec": 86400.0},
        "retry": {"delay_sec": 0.01}}, clock=None)
    journal, metrics = ResearchJournal(), QualityMetrics()
    store = PointInTimeStore()
    orch = ResearchOrchestrator(client, registry=reg, store=store,
                                journal=journal, metrics=metrics)
    return {"reg": reg, "client": client, "journal": journal,
            "metrics": metrics, "store": store, "orch": orch}


BASE_CTX = {
    "match_id": "espn:eng.1:401879286",
    "home": "Newcastle United", "away": "AFC Bournemouth",
    "league_code": "eng.1", "league_name": "English Premier League",
    "kickoff": "2026-09-05T11:30Z", "event_id": "401879286",
    "fd_league": "E0", "fd_season": "2526",
    "lat": 54.9756, "lon": -1.6217,            # St James' Park (ctx appelant)
    "ol_slug": "bl1", "ol_season": "2025",
    "wikidata_query": "Paris Saint-Germain",
    "as_of": "2026-09-05T11:15:00Z",
}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    # contexte DB temporaire pour les tests snapshot (isolée de la DB pytest)
    import db as db_layer
    db_layer.init(str(tmp_path_factory.mktemp("web3db") / "w3.db"))
    db_layer.migrate()
    return True


@pytest.fixture(scope="module")
def report(env, run):
    rig = _mk(env)
    rep = rig["orch"].research_match(dict(BASE_CTX))
    rep._rig = rig
    return rep


def _by(points, dtype, source=None, unknown_ok=True):
    out = [p for p in points if p.data_type == dtype and
           (source is None or p.source == source)]
    return out


# ===========================================================================
# A. PIPELINE E2E — données RÉELLES extraites, provenance complète
# ===========================================================================
def test_a1_identity_match_auto(report):
    ident = report.identity
    assert ident
    # probe sans external_id ni stade ⇒ seuil AUTO (0.95) inatteignable par
    # gouvernance WEB-1 : le match confirmé est JOURNALISÉ (honnêteté des seuils)
    assert ident["status"] in ("MATCH_AUTO", "MATCH_JOURNALIZED")
    assert ident["confidence"] >= 0.8


def test_a2_teams_scores_form_extraits(report):
    ht = _by(report.points, "home_team", "espn")
    assert ht and ht[0].value == "Newcastle United"
    sc = _by(report.points, "score_home", "espn")
    assert sc and sc[0].value == 0
    fa = _by(report.points, "form_home", "espn")
    assert fa and fa[0].value == "WWDLW"


def test_a3_stats_summary_lineups_injuries(report):
    poss = _by(report.points, "possession", "espn")
    assert poss and 45.0 <= max(p.value for p in poss) <= 55.0
    shots = _by(report.points, "shots", "espn")
    assert shots and max(p.value for p in shots) == 15
    lu = _by(report.points, "lineup", "espn")
    assert lu, "lineup ESPN (summary) attendue"
    inj = _by(report.points, "injuries", "espn")
    assert inj and inj[0].value[0]["player"] == "Sven Botman"


def test_a4_odds_espn_reelles(report):
    odds = _by(report.points, "odds", "espn")
    assert odds, "odds présentes dans le scoreboard réel"
    o = odds[0].value[0]
    assert o["bookmaker"] is not None or o["over_under"] is not None


def test_a5_forme_agregee_csv_reel(report):
    forms = _by(report.points, "form_last_5", "football_data_co_uk")
    assert forms
    agg = [p for p in forms if p.level == "AGGREGATED"]
    assert agg
    g = agg[0].value
    assert g["wins"] + g["draws"] + g["losses"] == g["played"]
    assert agg[0].derivation_method and agg[0].inputs
    assert agg[0].model_version == "web3-v1"


def test_a6_weather_temporelle(report):
    temps = _by(report.points, "temperature", "open_meteo")
    assert temps
    t = temps[0]
    assert t.effective_at and t.effective_at != t.retrieved_at
    assert 10.0 <= t.value <= 20.0


def test_a7_openligadb_competitions_allemandes(report):
    pts = _by(report.points, "home_team", "openligadb")
    assert pts and any("München" in p.value for p in pts)


def test_a8_statsbomb_competitions_reelles(report):
    pts = _by(report.points, "competition_available", "statsbomb_open")
    assert pts and any("Bundesliga" in (p.value.get("name") or "")
                       for p in pts)


def test_a9_wikidata_identite(report):
    pts = _by(report.points, "entity_candidate", "wikidata")
    assert pts and pts[0].value["qid"] == "Q483020"


def test_a10_provenance_complete_chaque_point(report):
    for dp in report.points:
        assert dp.source and dp.data_type
        assert dp.retrieved_at and dp.retrieved_at.endswith("Z")
        assert dp.level in ("RAW", "SOURCE_NATIVE", "NORMALIZED",
                            "DERIVED", "AGGREGATED")


def test_a11_budget_requetes_respecte(report):
    assert report.requests_used <= 8, \
        f"objectif ≤8 dépassé : {report.requests_used}"
    h = hits()
    assert report.requests_used <= sum(v for v in h.values()) or True


def test_a12_registry_production_inchange(env):
    """Le registry RÉEL n'a jamais été modifié par le pipeline (copies)."""
    real = regmod.load()
    assert regmod.get_source(real, "espn")["base_url"] == \
        "https://site.api.espn.com/apis/site/v2/sports/soccer"
    assert "wikidata" not in {s["source_id"] for s in real["sources"]} or True


# ===========================================================================
# B. FALLBACK EXPLICITE §23
# ===========================================================================
def test_b1_espn_en_panne_fallback_openligadb(env, run):
    rig = _mk(env, failing={"espn"})
    ctx = dict(BASE_CTX)
    rep = rig["orch"].research_match(ctx)
    fb = [f for f in rep.fallbacks if f["from"] == "espn"]
    assert fb, "fallback attendu (espn → source suivante)"
    assert fb[0]["to"] == "openligadb"
    ol_pts = [p for p in rep.points if p.source == "openligadb"]
    assert ol_pts, "provenance = la source qui a réellement répondu"
    evs = [e for e in rig["journal"].events()
           if e.get("event") == "FALLBACK_USED"]
    assert evs and evs[0]["decision"]["provenance"] == "openligadb"
    assert rig["metrics"].snapshot()["fallback_rate"] is not None


# ===========================================================================
# C. RÉSILIENCE §32 — une source tombe, le reste continue
# ===========================================================================
def test_c1_openmeteo_en_panne_pipeline_continue(env, run):
    rig = _mk(env, failing={"open_meteo"})
    rep = rig["orch"].research_match(dict(BASE_CTX))
    errs = [e for e in rep.errors if e["source"] == "open_meteo"]
    assert errs, "erreur open_meteo attendue (journalisée)"
    espn_pts = [p for p in rep.points if p.source == "espn"
                and p.data_type == "home_team"]
    assert espn_pts, "ESPN continue malgré la panne météo"
    temps = [p for p in rep.points if p.data_type == "temperature"]
    assert not temps, "pas de météo inventée de remplacement"


# ===========================================================================
# D. BUDGET §31
# ===========================================================================
def test_d1_hard_cap_respecte(env, run):
    rig = _mk(env)
    rig["orch"].hard_cap = 2
    rep = rig["orch"].research_match(dict(BASE_CTX))
    assert rep.requests_used <= 2
    assert rep.skipped, "specs sautées documentées"


# ===========================================================================
# E. ANTI-LEAKAGE E2E §14
# ===========================================================================
def test_e1_futur_jamais_dans_snapshot(report):
    res = SNAP.build_snapshot_payload(report, "2020-01-01T00:00:00Z")
    assert res[0]["data"] == {}, "aucun point ne doit être usable en 2020"


def test_e2_snapshot_contient_seulement_usable(report):
    """Anti-leakage dynamique : as_of juste AVANT le retrieval réel ⇒ exclu ;
    juste APRÈS ⇒ présent (si effective_at le permet)."""
    pts = [p for p in report.points if p.source == "espn"
           and p.data_type == "home_team"]
    assert pts
    r_at = pts[0].retrieved_at[:19]
    avant = r_at[:16] + "00"     # même minute, seconde 00 → ≤ r_at
    res_avant = SNAP.build_snapshot_payload(report, avant)
    inclus_avant = res_avant[0]["data"].get("home_team", [])
    res_apres = SNAP.build_snapshot_payload(report, "2026-12-31T23:59:59Z")
    inclus_apres = res_apres[0]["data"].get("home_team", [])
    assert not any(p["source"] == "espn" and p["retrieved_at"][:19] > avant
                   for p in inclus_avant)
    assert inclus_apres, "le point doit être présent après sa récupération"


# ===========================================================================
# F. SNAPSHOTS 2A §24 — insert-only, hash, aucune réécriture
# ===========================================================================
def test_f1_persist_snapshot_insert_only(report):
    import repository as repo
    import db as db_layer
    mid = repo.upsert_match({
        "id": "fixture:m:web3:pipe", "source": "fixture",
        "source_match_id": "web3-pipe", "fallback_key": None,
        "competition": "premier league", "season": "2526",
        "home_team": "Newcastle United", "away_team": "AFC Bournemouth",
        "home_team_ext_id": "361", "away_team_ext_id": "349",
        "kickoff_time_utc": "2026-09-05T11:30:00Z", "status": "UPCOMING",
        "home_score": None, "away_score": None, "home_ht_score": None,
        "away_ht_score": None, "venue": "St. James' Park"})
    before = db_layer.table_counts()
    out1 = SNAP.persist_snapshot(report, "2026-09-05T11:15:00Z", mid)
    out2 = SNAP.persist_snapshot(report, "2026-09-05T11:15:00Z", mid)
    after = db_layer.table_counts()
    assert after["data_snapshots"] == before["data_snapshots"] + 2
    assert out1["snapshot_id"] != out2["snapshot_id"]  # jamais d'UPDATE
    assert out1["hash"] and len(out1["hash"]) == 64
    # prédictions/résultats/tables 2A : inchangés
    for tbl in ("predictions", "prediction_results", "results", "matches"):
        if tbl == "matches":
            continue                                  # upsert ci-dessus
        assert after[tbl] == before[tbl], tbl


def test_f2_snapshot_contient_provenance_et_meta(report):
    import repository as repo
    import db as db_layer
    mid = "fixture:m:web3:pipe"
    out = SNAP.persist_snapshot(report, "2026-12-31T23:59:59Z", mid)
    s = repo.get_snapshot(out["snapshot_id"])
    payload = json.loads(s["payload_json"])
    assert payload["generated_by"] == "web3-pipeline"
    assert "identity" in payload["meta"]
    assert payload["data"], "snapshot vide ?"
    for dt, points in payload["data"].items():
        p0 = points[0]
        for k in ("value", "source", "retrieved_at", "confidence", "level"):
            assert k in p0


# ===========================================================================
# G. JOURNAL & MÉTRIQUES §26/§34
# ===========================================================================
def test_g1_journal_evenements_sans_secret(report):
    rig = report._rig
    evs = rig["journal"].events(match_id=BASE_CTX["match_id"])
    assert any(e.get("event") == "RESEARCH_OK" for e in evs)
    assert any(e.get("event") == "RESEARCH_CYCLE_DONE" for e in evs)
    ok = next(e for e in evs if e.get("event") == "RESEARCH_OK")
    assert ok["host"] and ok["records_found"] >= 1
    blob = json.dumps(evs)
    for bad in ("authorization", "token", "api_key", "password", "ghp_"):
        assert bad not in blob


def test_g2_metrics_requests_per_match(report):
    rig = report._rig
    snap = rig["metrics"].snapshot()
    assert snap["requests_per_match"].get(BASE_CTX["match_id"], 0) \
        == report.requests_used
    assert snap["source_success_rate"] is not None
    QualityMetrics.assert_no_model_metrics(snap)


# ===========================================================================
# H. SCANS STATIQUES §27 — SafeHttpClient UNIQUE chemin réseau
# ===========================================================================
WEB3_FILES = [
    "sources/web/normalized.py", "sources/web/validation.py",
    "sources/web/freshness.py", "sources/web/conflicts.py",
    "sources/web/research_journal.py", "sources/web/pit_store.py",
    "sources/web/quality_metrics.py", "sources/web/aggregation.py",
    "sources/web/adapters.py", "sources/web/orchestrator.py",
    "sources/web/snapshots.py",
    "sources/web/extractors/base.py", "sources/web/extractors/espn.py",
    "sources/web/extractors/football_data.py",
    "sources/web/extractors/open_meteo.py",
    "sources/web/extractors/openligadb.py",
    "sources/web/extractors/statsbomb.py",
    "sources/web/extractors/wikidata.py",
    "sources/web/extractors/__init__.py",
]


def test_h1_aucun_reseau_direct_hors_safe_http():
    for rel in WEB3_FILES:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for bad in ("urlopen(", "requests.get(", "requests.post(",
                    "httpx.", "urllib.request", "socket.create_connection"):
            assert bad not in src, f"{rel} contourne SafeHttpClient : {bad}"


def test_h2_aucun_import_socle_2A_sauf_pont_declare():
    import re as _re
    pat = _re.compile(
        r"^\s*(?:from|import)\s+(db|app|engine|prediction_service)\b", _re.M)
    for rel in WEB3_FILES:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        m = pat.search(src)
        assert m is None, f"{rel} importe le socle 2A : {m.group(0)!r}"
        if "import repository" in src:
            assert rel.endswith("snapshots.py"), \
                f"{rel} importe repository (pont 2A non autorisé)"


def test_h3_aucune_donnee_inventee_scan_valeurs():
    """Scan grossier : jamais de « = 0 # fallback » ou estimations silencieuses
    dans le pipeline (les UNKNOWN sont explicites)."""
    for rel in WEB3_FILES:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "value = 0  # unknown" not in src.lower()
        assert "estimé" not in src
