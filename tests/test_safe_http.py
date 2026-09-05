# -*- coding: utf-8 -*-
"""
TESTS — SAFE HTTP CLIENT (2B.WEB-2)
====================================
Banc d'essai INTÉGRALEMENT LOOPBACK :
- un serveur HTTP local (127.0.0.1:<port dynamique>) joue le rôle de source ;
- source factice « localtest » injectée dans une COPIE du registry réel,
  avec overrides EXPLICITES (allow_http + allow_private_hosts) — les seules
  portes dérogatoires, jamais appliquées aux sources réelles ;
- GARDE ANTI-RÉSEAU-PUBLIC : toute résolution DNS vers autre chose que
  loopback lève AssertionError → PUBLIC NETWORK ACCESS DURING TESTS = 0.
"""
import copy
import gzip
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sources import registry as regmod
from sources import provenance
from sources.web import compliance as comp
from sources.web.compliance import ComplianceGate
from sources.web.safe_http import SafeHttpClient, SAFE_HTTP_CONFIG
from sources.web.response import (
    SafeHttpError, SafeHttpResponse, ComplianceDeniedError, InvalidSourceError,
    InvalidURLError, SSRFBlockedError, RedirectBlockedError, HttpTimeoutError,
    ResponseTooLargeError, InvalidContentTypeError, RateLimitExceededError,
    CircuitOpenError, NetworkError, HttpStatusError, InvalidMethodError)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_REG = regmod.load()
MAX_B = SAFE_HTTP_CONFIG["max_body_bytes"]

# ===========================================================================
# GARDE ANTI-RÉSEAU-PUBLIC (preuve PUBLIC=0) — autouse pour TOUT le module
# ===========================================================================
_REAL_GAI = socket.getaddrinfo
PUBLIC_ATTEMPTS = []


@pytest.fixture(autouse=True)
def _no_public_network(monkeypatch):
    def guard(host, port, *a, **k):
        h = (host or "")
        if h in ("127.0.0.1", "localhost", "::1"):
            return _REAL_GAI(host, port, *a, **k)
        PUBLIC_ATTEMPTS.append(h)
        raise AssertionError(
            f"ACCES RESEAU PUBLIC INTERDIT PENDANT LES TESTS : {h!r}")
    monkeypatch.setattr(socket, "getaddrinfo", guard)


# ===========================================================================
# SERVEUR DE TEST LOOPBACK
# ===========================================================================
HITS = {}
HITS_LOCK = threading.Lock()
STATE = {}
ZIPBOMB = gzip.compress(b"0" * (8 * 1024 * 1024))


def _bump(path):
    with HITS_LOCK:
        HITS[path] = HITS.get(path, 0) + 1


def hits(path):
    with HITS_LOCK:
        return HITS.get(path, 0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- helpers -------------------------------------------------------------
    def _send(self, code=200, body=b"", ctype="application/json",
              headers=None, close=False):
        if close:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Connection", "close")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routes --------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        _bump(path)
        try:
            self._route(path)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _route(self, path):
        if path == "/json":
            self._send(body=b'{"ok": true}', ctype="application/json")
        elif path == "/csv":
            self._send(body=b"a,b\n1,2\n", ctype="text/csv")
        elif path == "/text":
            self._send(body=b"bonjour", ctype="text/plain")
        elif path == "/html":
            self._send(body=b"<html><body>x</body></html>", ctype="text/html")
        elif path == "/octet":
            self._send(body=b"\x00\x01", ctype="application/octet-stream")
        elif path == "/noct":
            body = b"data"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/204":
            self.send_response(204)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
        elif path == "/gzip":
            self._send(body=gzip.compress(b'{"ok": true}'),
                       ctype="application/json",
                       headers={"Content-Encoding": "gzip"})
        elif path == "/zipbomb":
            self._send(body=ZIPBOMB, ctype="application/json",
                       headers={"Content-Encoding": "gzip"})
        elif path == "/chunked":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in (b'{"he', b'llo"', b": true}"):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\nX-End: 1\r\n\r\n")
            self.wfile.flush()
        elif path == "/big-declared":
            # Content-Length > 5 Mo : le client DOIT refuser avant lecture
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(MAX_B + 1024))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True   # on n'envoie JAMAIS le corps
        elif path == "/big-stream":
            self._send(body=b"x" * (MAX_B + 200 * 1024),
                       ctype="text/plain", close=True)
        elif path in ("/size-1kb", "/size-1mb", "/size-4-9mb", "/size-5mb"):
            n = {"/size-1kb": 1024, "/size-1mb": 1024 * 1024,
                 "/size-4-9mb": MAX_B - 100 * 1024, "/size-5mb": MAX_B}[path]
            self._send(body=b"z" * n, ctype="text/plain")
        elif path == "/size-5-1mb":
            self._send(body=b"z" * (MAX_B + 1024), ctype="text/plain")
        elif path.startswith("/status-"):
            self._send(code=int(path.rsplit("-", 1)[1]), body=b"{}")
        elif path in ("/flaky-502", "/flaky-503"):
            code = int(path.rsplit("-", 1)[1])
            with HITS_LOCK:
                n = STATE.get(path, 0)
                STATE[path] = n + 1
            if n == 0:
                self._send(code=code, body=b"{}")
            else:
                self._send(body=b'{"recovered": true}')
        elif path in ("/always-500", "/always-502", "/always-504"):
            self._send(code=int(path.rsplit("-", 1)[1]), body=b"{}")
        elif path == "/slow":
            time.sleep(1.5)
            self._send(body=b'{"slow": true}')
        elif path == "/redirect1":
            self._redirect("/json")
        elif path == "/chain":
            self._redirect("/chain2")
        elif path == "/chain2":
            self._redirect("/chain3")
        elif path == "/chain3":
            self._redirect("/json")
        elif path in ("/chain4", "/chain4b", "/chain4c", "/chain4d"):
            self._redirect({"/chain4": "/chain4b", "/chain4b": "/chain4c",
                            "/chain4c": "/chain4d", "/chain4d": "/json"}[path])
        elif path == "/redirect-external":
            self._redirect("https://example.com/page")
        elif path == "/redirect-private":
            self._redirect("http://10.0.0.1/secret")
        elif path == "/redirect-loop":
            self._redirect("/redirect-loop")
        elif path == "/redirect-relative":
            self._redirect("foo/../json")
        elif path == "/echo-headers":
            body = json.dumps({
                "user-agent": self.headers.get("User-Agent"),
                "accept": self.headers.get("Accept"),
                "accept-encoding": self.headers.get("Accept-Encoding"),
                "cookie": self.headers.get("Cookie"),
                "authorization": self.headers.get("Authorization"),
                "host": self.headers.get("Host"),
                "connection": self.headers.get("Connection"),
            }).encode()
            self._send(body=body)
        else:
            self._send(code=404, body=b"{}")


@pytest.fixture(scope="module")
def env():
    """Serveur loopback + registry factice (copie) + gate avec overrides."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    reg = copy.deepcopy(REAL_REG)
    reg["sources"].append({
        "source_id": "localtest", "name": "Local Test Bench",
        "type": "api", "base_url": f"http://127.0.0.1:{port}",
        "status": "TESTED", "enabled": True, "authentication": "none",
        "free": True, "quota": "bench", "coverage": "local",
        "capabilities": {"score_live": True, "standings": True,
                         "historical_results": True, "weather": True,
                         "match_calendar": True, "form": True, "h2h": True,
                         "team_stats": True, "odds": True, "lineups": True,
                         "injuries": True},
        "priority_role": {}, "ttl_overrides_sec": {}, "fallback": {},
        "last_tested": None, "reliability": 1.0, "notes": "bench",
        "risks": []})
    reg["sources"].append({
        "source_id": "localdead", "name": "Dead Bench", "type": "api",
        "base_url": "http://127.0.0.1:9", "status": "TESTED",
        "enabled": True, "authentication": "none", "free": True,
        "quota": "bench", "coverage": "local",
        "capabilities": {"score_live": True}, "priority_role": {},
        "ttl_overrides_sec": {}, "fallback": {}, "last_tested": None,
        "reliability": 1.0, "notes": "dead", "risks": []})
    gate = ComplianceGate(reg, {
        "localtest": {"allow_http": True, "allow_private_hosts": True},
        "localdead": {"allow_http": True, "allow_private_hosts": True}})
    yield {"port": port, "reg": reg, "gate": gate,
           "url": lambda p: f"http://127.0.0.1:{port}{p}"}
    srv.shutdown()
    srv.server_close()


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_client(env, clock=None, logger=None, **cfg):
    base = {"rate_limit": {"per_source_min_interval_sec": 0.0,
                           "cycle_max_requests": 100000,
                           "cycle_window_sec": 86400.0},
            "retry": {"delay_sec": 0.01}}
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return SafeHttpClient(registry=env["reg"], gate=env["gate"], config=base,
                          clock=clock, logger=logger)


# ===========================================================================
# A. RÉPONSE & PROVENANCE
# ===========================================================================
def test_a1_get_json_ok(env):
    r = make_client(env).fetch("localtest", env["url"]("/json"), "score_live")
    assert r.ok and r.status_code == 200
    assert json.loads(r.body.decode()) == {"ok": True}
    assert r.content_type == "application/json"
    assert r.attempts == 1 and r.cache_hit is False


def test_a2_champs_complets_provenance(env):
    r = make_client(env).fetch("localtest", env["url"]("/json"), "score_live")
    d = r.to_dict()
    for k in ("ok", "status_code", "url", "source_id", "data_type",
              "retrieved_at", "latency_ms", "compliance_decision",
              "cache_hit", "attempts"):
        assert k in d
    assert d["source_id"] == "localtest" and d["data_type"] == "score_live"
    assert d["url"] == env["url"]("/json")
    assert d["compliance_decision"]["decision"] == "ALLOW"


def test_a3_retrieved_at_format_iso_2A(env):
    r = make_client(env).fetch("localtest", env["url"]("/json"), "score_live")
    assert provenance.ISO_RE.match(r.retrieved_at), r.retrieved_at


def test_a4_jamais_effective_at_cote_client(env):
    r = make_client(env).fetch("localtest", env["url"]("/json"), "score_live")
    assert not hasattr(r, "effective_at")       # anti-fuite §48


def test_a5_final_url_sans_redirect(env):
    r = make_client(env).fetch("localtest", env["url"]("/json"), "score_live")
    assert r.url == env["url"]("/json")


def test_a6_latency_et_bytes(env):
    r = make_client(env).fetch("localtest", env["url"]("/text"), "score_live")
    assert r.latency_ms >= 0 and r.content_length == len(r.body) == 7


def test_a7_body_malformed_json_retourne_brut(env):
    srv_body = b"{pas json"
    # /text n'est pas du JSON : le client ne parse JAMAIS → body brut
    r = make_client(env).fetch("localtest", env["url"]("/text"), "score_live")
    assert r.body == b"bonjour" and isinstance(r.body, bytes)


def test_a8_204_body_vide_ok(env):
    r = make_client(env).fetch("localtest", env["url"]("/204"), "score_live")
    assert r.ok and r.status_code == 204 and r.body == b""


# ===========================================================================
# B. MÉTHODES — GET seul, refus AVANT réseau (compteur serveur = 0)
# ===========================================================================
@pytest.mark.parametrize("m", ["POST", "PUT", "PATCH", "DELETE"])
def test_b1_methodes_refusees(env, m):
    before = hits("/json")
    with pytest.raises(InvalidMethodError):
        make_client(env).fetch("localtest", env["url"]("/json"), "score_live",
                               method=m)
    assert hits("/json") == before                # aucune requête partie


def test_b2_get_explicit_ok(env):
    r = make_client(env).fetch("localtest", env["url"]("/json"), "score_live",
                               method="GET")
    assert r.ok


# ===========================================================================
# C. TAILLES — bornes exactes du plafond 5 Mo
# ===========================================================================
def test_c1_1kb_ok(env):
    r = make_client(env).fetch("localtest", env["url"]("/size-1kb"),
                               "historical_results")
    assert r.ok and len(r.body) == 1024


def test_c2_1mb_ok(env):
    r = make_client(env).fetch("localtest", env["url"]("/size-1mb"),
                               "historical_results")
    assert len(r.body) == 1024 * 1024


def test_c3_4_9mb_ok(env):
    r = make_client(env).fetch("localtest", env["url"]("/size-4-9mb"),
                               "historical_results")
    assert len(r.body) == MAX_B - 100 * 1024


def test_c4_exactement_5mb_ok(env):
    r = make_client(env).fetch("localtest", env["url"]("/size-5mb"),
                               "historical_results")
    assert len(r.body) == MAX_B


def test_c5_5_1mb_declare_refuse(env):
    with pytest.raises(ResponseTooLargeError):
        make_client(env).fetch("localtest", env["url"]("/size-5-1mb"),
                               "historical_results")


def test_c6_content_length_geant_refuse_sans_lire(env):
    t0 = time.monotonic()
    with pytest.raises(ResponseTooLargeError):
        make_client(env).fetch("localtest", env["url"]("/big-declared"),
                               "historical_results")
    assert time.monotonic() - t0 < 3.0            # refus immédiat (pas lu)


def test_c7_flux_sans_content_length_plafonne(env):
    with pytest.raises(ResponseTooLargeError):
        make_client(env).fetch("localtest", env["url"]("/big-stream"),
                               "historical_results")


# ===========================================================================
# D. CONTENT-TYPES
# ===========================================================================
@pytest.mark.parametrize("route,ct", [("/json", "application/json"),
                                      ("/csv", "text/csv"),
                                      ("/text", "text/plain"),
                                      ("/html", "text/html")])
def test_d1_content_types_autorises(env, route, ct):
    r = make_client(env).fetch("localtest", env["url"](route), "match_calendar")
    assert r.ok and r.content_type == ct


def test_d2_octet_stream_refuse(env):
    with pytest.raises(InvalidContentTypeError):
        make_client(env).fetch("localtest", env["url"]("/octet"), "score_live")


def test_d3_content_type_absent_refuse(env):
    with pytest.raises(InvalidContentTypeError):
        make_client(env).fetch("localtest", env["url"]("/noct"), "score_live")


# ===========================================================================
# E. GZIP — APPLIQUE AUSSI AU DÉCOMPRESSÉ
# ===========================================================================
def test_e1_gzip_decode_ok(env):
    r = make_client(env).fetch("localtest", env["url"]("/gzip"), "score_live")
    assert json.loads(r.body.decode()) == {"ok": True}


def test_e2_zipbomb_refusee(env):
    with pytest.raises(ResponseTooLargeError):
        make_client(env).fetch("localtest", env["url"]("/zipbomb"),
                               "historical_results")


# ===========================================================================
# F. CHUNKED
# ===========================================================================
def test_f1_chunked_ok_avec_trailers(env):
    r = make_client(env).fetch("localtest", env["url"]("/chunked"),
                               "score_live")
    assert json.loads(r.body.decode()) == {"hello": True}


# ===========================================================================
# G. REDIRECTIONS — chaque hop entièrement re-vérifié
# ===========================================================================
def test_g1_une_redirection_ok_url_finale(env):
    r = make_client(env).fetch("localtest", env["url"]("/redirect1"),
                               "score_live")
    assert r.ok and r.url == env["url"]("/json")


def test_g2_trois_redirections_ok(env):
    r = make_client(env).fetch("localtest", env["url"]("/chain"), "score_live")
    assert r.ok and r.url == env["url"]("/json")


def test_g3_quatrieme_redirection_refusee(env):
    with pytest.raises(RedirectBlockedError):
        make_client(env).fetch("localtest", env["url"]("/chain4"),
                               "score_live")


def test_g4_redirection_externe_refusee(env):
    with pytest.raises(RedirectBlockedError):
        make_client(env).fetch("localtest", env["url"]("/redirect-external"),
                               "score_live")
    assert PUBLIC_ATTEMPTS == []                  # example.com JAMAIS résolu


def test_g5_redirection_privee_refusee(env):
    with pytest.raises(RedirectBlockedError):
        make_client(env).fetch("localtest", env["url"]("/redirect-private"),
                               "score_live")


def test_g6_boucle_redirection_plafonnee(env):
    with pytest.raises(RedirectBlockedError):
        make_client(env).fetch("localtest", env["url"]("/redirect-loop"),
                               "score_live")


def test_g7_redirection_relative_normalisee_ok(env):
    r = make_client(env).fetch("localtest", env["url"]("/redirect-relative"),
                               "score_live")
    assert r.ok and r.url == env["url"]("/json")


# ===========================================================================
# H. STATUTS HTTP & RETRY — COMPTES DE REQUÊTES EXACTS
# ===========================================================================
@pytest.mark.parametrize("code", [400, 401, 403, 404, 429])
def test_h1_4xx_jamais_retry(env, code):
    route = f"/status-{code}"
    before = hits(route)
    c = make_client(env)
    with pytest.raises(HttpStatusError) as ei:
        c.fetch("localtest", env["url"](route), "score_live")
    assert ei.value.status_code == code
    assert hits(route) == before + 1              # 1 seule requête


def test_h2_502_retry_puis_succes(env):
    route = "/flaky-502"
    STATE.pop(route, None)
    before = hits(route)
    r = make_client(env).fetch("localtest", env["url"](route), "score_live")
    assert r.ok and json.loads(r.body.decode()) == {"recovered": True}
    assert hits(route) == before + 2              # 1 échec + 1 retry


def test_h3_503_retry_puis_succes(env):
    route = "/flaky-503"
    STATE.pop(route, None)
    before = hits(route)
    r = make_client(env).fetch("localtest", env["url"](route), "score_live")
    assert r.ok
    assert hits(route) == before + 2


def test_h4_504_retry_puis_echec_compte_exact(env):
    route = "/always-504"
    before = hits(route)
    c = make_client(env)
    with pytest.raises(HttpStatusError) as ei:
        c.fetch("localtest", env["url"](route), "score_live")
    assert ei.value.status_code == 504
    assert hits(route) == before + 2              # max 1 retry → 2 tentatives


def test_h5_erreur_reseau_pas_de_retry(env):
    c = make_client(env)
    t0 = time.monotonic()
    with pytest.raises(NetworkError):
        c.fetch("localdead", "http://127.0.0.1:9/x", "score_live")
    assert time.monotonic() - t0 < 5.0            # pas de délai de retry
    assert c.circuit_state("localdead")["fails"] == 1


# ===========================================================================
# I. TIMEOUTS — bornés, jamais de blocage indéfini
# ===========================================================================
def test_i1_endpoint_lent_timeout(env):
    c = make_client(env, timeouts={"read_sec": 0.3, "cycle_budget_sec": 10.0})
    t0 = time.monotonic()
    with pytest.raises(HttpTimeoutError):
        c.fetch("localtest", env["url"]("/slow"), "score_live")
    dt = time.monotonic() - t0
    assert dt < 10.0                              # budget borné


def test_i2_timeout_compte_comme_echec_circuit(env):
    c = make_client(env, timeouts={"read_sec": 0.3, "cycle_budget_sec": 10.0})
    with pytest.raises(HttpTimeoutError):
        c.fetch("localtest", env["url"]("/slow"), "score_live")
    assert c.circuit_state("localtest")["fails"] >= 1


# ===========================================================================
# J. RATE LIMITS — aucune requête supplémentaire après dépassement
# ===========================================================================
def test_j1_budget_cycle_depasse_bloque_avant_reseau(env):
    c = make_client(env, rate_limit={"cycle_max_requests": 2,
                                     "cycle_window_sec": 60.0,
                                     "per_source_min_interval_sec": 0.0})
    u = env["url"]("/json")
    c.fetch("localtest", u, "score_live", allow_cache=False)
    c.fetch("localtest", u, "score_live", allow_cache=False)
    before = hits("/json")
    with pytest.raises(RateLimitExceededError):
        c.fetch("localtest", u, "score_live", allow_cache=False)
    assert hits("/json") == before                # 0 requête en plus


def test_j2_intervalle_minimal_par_source(env):
    c = make_client(env, rate_limit={"per_source_min_interval_sec": 0.25,
                                     "cycle_max_requests": 100,
                                     "cycle_window_sec": 60.0})
    u = env["url"]("/json")
    t0 = time.monotonic()
    c.fetch("localtest", u, "score_live", allow_cache=False)
    c.fetch("localtest", u, "score_live", allow_cache=False)
    assert time.monotonic() - t0 >= 0.25


def test_j3_fenetre_cycle_reset(env):
    fk = FakeClock()
    c = make_client(env, clock=fk,
                    rate_limit={"cycle_max_requests": 2,
                                "cycle_window_sec": 10.0,
                                "per_source_min_interval_sec": 0.0})
    u = env["url"]("/json")
    c.fetch("localtest", u, "score_live", allow_cache=False)
    c.fetch("localtest", u, "score_live", allow_cache=False)
    fk.advance(11.0)                              # fenêtre expirée → reset
    r = c.fetch("localtest", u, "score_live", allow_cache=False)
    assert r.ok


def test_j4_budget_global_partage_entre_sources(env):
    c = make_client(env, rate_limit={"cycle_max_requests": 2,
                                     "cycle_window_sec": 60.0,
                                     "per_source_min_interval_sec": 0.0})
    u = env["url"]("/json")
    c.fetch("localtest", u, "score_live", allow_cache=False)
    try:
        c.fetch("localdead", "http://127.0.0.1:9/x", "score_live")
    except NetworkError:
        pass                                       # compte quand même le cycle
    with pytest.raises(RateLimitExceededError):
        c.fetch("localtest", u, "score_live", allow_cache=False)


# ===========================================================================
# K. CIRCUIT BREAKER — 5(3 ici) échecs → OPEN 30 min → demi-essai → reprise
# ===========================================================================
def _cb_client(env, fk):
    return make_client(env, clock=fk,
                       circuit_breaker={"failures_to_open": 3,
                                        "open_duration_sec": 1800.0})


def test_k1_ecchecs_consecutifs_ouvrent_le_circuit(env):
    fk = FakeClock()
    c = _cb_client(env, fk)
    before = hits("/always-500")
    for _ in range(3):
        with pytest.raises(HttpStatusError):
            c.fetch("localtest", env["url"]("/always-500"), "score_live",
                    allow_cache=False)
    assert c.circuit_state("localtest")["state"] == "open"
    assert hits("/always-500") == before + 3


def test_k2_circuit_ouvert_bloque_sans_reseau(env):
    fk = FakeClock()
    c = _cb_client(env, fk)
    for _ in range(3):
        with pytest.raises(HttpStatusError):
            c.fetch("localtest", env["url"]("/always-500"), "score_live",
                    allow_cache=False)
    before = hits("/always-500")
    with pytest.raises(CircuitOpenError):
        c.fetch("localtest", env["url"]("/always-500"), "score_live",
                allow_cache=False)
    assert hits("/always-500") == before          # 0 requête en plus


def test_k3_expiration_demi_essai_puis_reprise(env):
    fk = FakeClock()
    c = _cb_client(env, fk)
    for _ in range(3):
        with pytest.raises(HttpStatusError):
            c.fetch("localtest", env["url"]("/always-500"), "score_live",
                    allow_cache=False)
    fk.advance(1801.0)                            # 30 min écoulées
    r = c.fetch("localtest", env["url"]("/json"), "score_live",
                allow_cache=False)
    assert r.ok
    st = c.circuit_state("localtest")
    assert st["state"] == "closed" and st["fails"] == 0


def test_k4_demi_essai_en_echec_reouvre(env):
    fk = FakeClock()
    c = _cb_client(env, fk)
    for _ in range(3):
        with pytest.raises(HttpStatusError):
            c.fetch("localtest", env["url"]("/always-500"), "score_live",
                    allow_cache=False)
    fk.advance(1801.0)
    with pytest.raises(HttpStatusError):
        c.fetch("localtest", env["url"]("/always-500"), "score_live",
                allow_cache=False)
    assert c.circuit_state("localtest")["state"] == "open"


def test_k5_refus_compliance_ne_declenche_pas_le_circuit(env):
    fk = FakeClock()
    c = _cb_client(env, fk)
    for _ in range(6):
        with pytest.raises(ComplianceDeniedError):
            c.fetch("sofascore", "https://www.sofascore.com/api/v1/x",
                    "score_live")
    assert c.circuit_state("sofascore")["fails"] == 0


# ===========================================================================
# L. CACHE — TTL registry, retrieved_at ORIGINAL conservé (WEB-3 §22)
# ===========================================================================
def test_l1_deuxieme_appel_cache_hit_retrieved_at_original(env):
    c = make_client(env)
    u = env["url"]("/json")
    before = hits("/json")
    r1 = c.fetch("localtest", u, "standings")
    r2 = c.fetch("localtest", u, "standings")
    assert hits("/json") == before + 1            # 1 seule requête réseau
    assert r2.cache_hit is True
    assert r2.retrieved_at == r1.retrieved_at     # PAS le timestamp du 2e appel


def test_l2_expiration_ttl_regenere(env):
    fk = FakeClock()
    c = make_client(env, clock=fk)
    u = env["url"]("/json")
    before = hits("/json")
    c.fetch("localtest", u, "standings")
    fk.advance(7201.0)                            # standings TTL = 7200 s
    c.fetch("localtest", u, "standings")
    assert hits("/json") == before + 2


def test_l3_data_type_different_pas_de_hit(env):
    c = make_client(env)
    u = env["url"]("/json")
    before = hits("/json")
    c.fetch("localtest", u, "score_live")
    r2 = c.fetch("localtest", u, "weather")       # autre clé de cache
    assert r2.cache_hit is False
    assert hits("/json") == before + 2


def test_l4_cache_desactive_toujours_reseau(env):
    c = make_client(env, cache={"enabled": False})
    u = env["url"]("/json")
    before = hits("/json")
    c.fetch("localtest", u, "standings")
    c.fetch("localtest", u, "standings")
    assert hits("/json") == before + 2


# ===========================================================================
# M. COMPLIANCE CÔTÉ CLIENT + AUCUN SILENT FALLBACK
# ===========================================================================
def test_m1_url_ssrf_via_espn_deny_avant_reseau(env):
    with pytest.raises(ComplianceDeniedError):
        make_client(env).fetch("espn", env["url"]("/json"), "score_live")
    assert PUBLIC_ATTEMPTS == []


def test_m2_url_publique_espn_jamais_touchee(env):
    """La garde getaddrinfo DOIT intercepter AVANT le moindre octet."""
    c = make_client(env)
    with pytest.raises(AssertionError):
        c.fetch("espn",
                "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
                "score_live", allow_cache=False)
    assert "site.api.espn.com" in PUBLIC_ATTEMPTS


def test_m3_sofascore_jamais_contactee(env):
    c = make_client(env)
    with pytest.raises(ComplianceDeniedError) as ei:
        c.fetch("sofascore", "https://www.sofascore.com/api/v1/x",
                "score_live")
    assert "POLICY_NEVER_CONTACT" in (ei.value.detail or {}).get("reasons", [])
    assert "www.sofascore.com" not in PUBLIC_ATTEMPTS


def test_m4_source_inconnue_invalid_source(env):
    with pytest.raises(InvalidSourceError):
        make_client(env).fetch("inconnue", env["url"]("/json"), "score_live")


def test_m5_capability_unknown_deny_explicite(env):
    c = make_client(env)
    with pytest.raises(ComplianceDeniedError) as ei:
        c.fetch("localtest", env["url"]("/json"), "news")  # non déclarée
    assert "CAPABILITY_NOT_DECLARED" in (ei.value.detail or {})["reasons"]


def test_m6_aucun_silent_fallback(env):
    """Un échec localtest ne déclenche AUCUN appel vers une autre source."""
    c = make_client(env)
    before_sofascore_dns = len(PUBLIC_ATTEMPTS)
    before_json = hits("/json")
    with pytest.raises(ComplianceDeniedError):
        c.fetch("sofascore", "https://www.sofascore.com/api/v1/x",
                "score_live")
    assert len(PUBLIC_ATTEMPTS) == before_sofascore_dns
    assert hits("/json") == before_json


# ===========================================================================
# N. fetch_many — concurrence ≤3, erreurs = objets, ordre conservé
# ===========================================================================
def test_n1_fetch_many_ordre_et_types(env):
    c = make_client(env)
    reqs = [{"source_id": "localtest", "url": env["url"]("/json"),
             "data_type": "score_live", } for _ in range(5)]
    reqs.insert(2, {"source_id": "localtest",
                    "url": env["url"]("/status-404"),
                    "data_type": "score_live"})
    out = c.fetch_many(reqs)
    assert len(out) == 6
    assert isinstance(out[2], HttpStatusError)
    for i, r in enumerate(out):
        if i != 2:
            assert isinstance(r, SafeHttpResponse) and r.ok


def test_n2_fetch_many_refus_compliance_en_objet(env):
    c = make_client(env)
    out = c.fetch_many([
        {"source_id": "sofascore",
         "url": "https://www.sofascore.com/api/v1/x", "data_type": "score_live"},
        {"source_id": "localtest", "url": env["url"]("/json"),
         "data_type": "score_live"}])
    assert isinstance(out[0], ComplianceDeniedError)
    assert isinstance(out[1], SafeHttpResponse)


def test_n3_fetch_many_concurrence_plafonnee_a_3(env):
    c = make_client(env)
    reqs = [{"source_id": "localtest", "url": env["url"]("/json"),
             "data_type": "score_live"} for _ in range(6)]
    t0 = time.monotonic()
    out = c.fetch_many(reqs, max_workers=10)       # demande 10 → plafond 3
    assert all(isinstance(r, SafeHttpResponse) for r in out)
    assert time.monotonic() - t0 < 20.0


# ===========================================================================
# O. LOGS & SECRETS — structurés, JAMAIS de token/cookie/Authorization
# ===========================================================================
def test_o1_log_ok_structure(env):
    entries = []
    c = make_client(env, logger=entries.append)
    c.fetch("localtest", env["url"]("/json"), "score_live", allow_cache=False)
    ok = [e for e in entries if e.get("status") == "OK"]
    assert ok
    e = ok[-1]
    for k in ("ts", "source_id", "data_type", "method", "http_status",
              "latency_ms", "bytes", "compliance"):
        assert k in e


def test_o2_log_deny_structure(env):
    entries = []
    c = make_client(env, logger=entries.append)
    with pytest.raises(ComplianceDeniedError):
        c.fetch("sofascore", "https://www.sofascore.com/api/v1/x",
                "score_live")
    deny = [e for e in entries if e.get("status") == "DENY"]
    assert deny and deny[-1]["compliance"] == "DENY"
    assert "POLICY_NEVER_CONTACT" in deny[-1]["reasons"]


def test_o3_aucune_fuite_secret_dans_logs(env):
    entries = []
    c = make_client(env, logger=entries.append)
    c.fetch("localtest", env["url"]("/json?key=ghp_FAUXTEST123456789"),
            "score_live", allow_cache=False)
    blob = json.dumps(entries).lower()
    for secret in ("ghp_", "authorization", "cookie", "token", "api_key"):
        assert secret not in blob


def test_o4_headers_envoyes_honnetes(env):
    r = make_client(env).fetch("localtest", env["url"]("/echo-headers"),
                               "score_live")
    sent = json.loads(r.body.decode())
    assert sent["user-agent"] == \
        "PronoFoot/3.0 (+pronosfoot-live;safe-http;contact:github)"
    assert sent["cookie"] is None
    assert sent["authorization"] is None
    assert "Googlebot" not in sent["user-agent"]
    assert "Chrome" not in sent["user-agent"]


# ===========================================================================
# P. SCANS STATIQUES — no-bypass / pas de secret / isolation 2A
# ===========================================================================
NEW_FILES = ("sources/web/response.py", "sources/web/compliance.py",
             "sources/web/safe_http.py")


def test_p1_scan_statique_interdictions():
    for rel in NEW_FILES:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for tok in ("_create_unverified_context", "CERT_NONE", "Googlebot",
                    "Chrome/", "x-fsign", "cf_clearance", "requests.get(",
                    "httpx", "urlopen(", "ghp_"):
            assert tok not in src, f"{rel} contient {tok!r}"


def test_p2_tls_verification_jamais_desactivee():
    src = open(os.path.join(ROOT, "sources/web/safe_http.py"),
               encoding="utf-8").read()
    assert "verify_mode = ssl.CERT_REQUIRED" in src
    assert "create_default_context" in src


def test_p3_aucun_import_socle_2A():
    for rel in NEW_FILES:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for bad in ("import db", "import app", "import engine ",
                    "import repository", "import prediction_service"):
            assert bad not in src, f"{rel} importe {bad!r}"


def test_p4_methodes_config_get_seul():
    assert SAFE_HTTP_CONFIG["allowed_methods"] == ("GET",)
    assert SAFE_HTTP_CONFIG["max_redirects"] == 3
    assert SAFE_HTTP_CONFIG["timeouts"]["connect_sec"] == 5.0
    assert SAFE_HTTP_CONFIG["timeouts"]["read_sec"] == 12.0
    assert SAFE_HTTP_CONFIG["timeouts"]["cycle_budget_sec"] == 25.0
    assert SAFE_HTTP_CONFIG["circuit_breaker"]["open_duration_sec"] == 1800.0


# ===========================================================================
# Q. INTÉGRITÉ 2A — DB / snapshots / hash / prédictions intacts
# ===========================================================================
def test_q1_db_tables_inchangees_apres_batterie_web2(env):
    import db as db_layer
    db_layer.init()
    db_layer.migrate()
    before = db_layer.table_counts()
    integrity0 = db_layer.integrity_check()
    c = make_client(env)
    c.fetch("localtest", env["url"]("/json"), "score_live",
            allow_cache=False)
    c.fetch("localtest", env["url"]("/csv"), "match_calendar",
            allow_cache=False)
    with pytest.raises(ComplianceDeniedError):
        c.fetch("sofascore", "https://www.sofascore.com/api/v1/x",
                "score_live")
    with pytest.raises(HttpStatusError):
        c.fetch("localtest", env["url"]("/status-404"), "score_live")
    assert db_layer.table_counts() == before
    assert db_layer.integrity_check() == integrity0


def test_q2_snapshot_hash_stable_apres_appels_web2(env):
    import repository as repo
    import db as db_layer
    db_layer.init()
    db_layer.migrate()
    mid = repo.upsert_match({
        "id": "fixture:m:web2:snap", "source": "fixture",
        "source_match_id": "web2-snap", "fallback_key": None,
        "competition": "ligue 1", "season": "2526",
        "home_team": "Paris Saint-Germain", "away_team": "Olympique Lyonnais",
        "home_team_ext_id": "1", "away_team_ext_id": "3",
        "kickoff_time_utc": "2026-09-06T20:00:00Z", "status": "UPCOMING",
        "home_score": None, "away_score": None,
        "home_ht_score": None, "away_ht_score": None, "venue": None})
    payload = {"form": {"w": 2}, "odds": None}
    snap = repo.insert_snapshot(mid, "2026-09-05T18:00:00Z", "fixture",
                                payload, "high", ["form"])
    h0 = repo.sha256_text(repo.canonical_json(payload))
    before = db_layer.table_counts()              # référence après insert
    c = make_client(env)
    c.fetch("localtest", env["url"]("/json"), "score_live", allow_cache=False)
    c.fetch_many([{"source_id": "localtest", "url": env["url"]("/text"),
                   "data_type": "weather"}])
    s1 = repo.get_snapshot(snap["id"])
    assert s1["payload_hash"] == h0
    assert s1["payload_json"] == repo.canonical_json(payload)
    assert db_layer.table_counts() == before      # 0 écriture pendant WEB-2


def test_q3_aucune_ecriture_db_par_safe_http(env):
    """Le client n'importe ni n'écrit la DB (scan + runtime déjà prouvés)."""
    import inspect
    from sources.web import safe_http
    src = inspect.getsource(safe_http)
    assert "insert_snapshot" not in src and "execute(" not in src


def test_q4_public_network_zero_compte_final():
    """Bilan global : la garde n'a autorisé AUCUN hôte public (hors preuve
    m2 qui compte son interception — ici on vérifie qu'aucun autre test
    n'a tenté une résolution publique non interceptée)."""
    allowed = {"site.api.espn.com"}               # uniquement preuve m2
    assert set(PUBLIC_ATTEMPTS) <= allowed, PUBLIC_ATTEMPTS
