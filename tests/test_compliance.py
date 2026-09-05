# -*- coding: utf-8 -*-
"""
TESTS — COMPLIANCE GATE (2B.WEB-2)
===================================
100 % HORS LIGNE : aucun réseau, aucune fixture externe — la gate est une
fonction PURE du registry réel (ou de copies modifiées en mémoire).
"""
import copy
import os

import pytest

from sources import registry as regmod
from sources.web import compliance as comp
from sources.web.compliance import (
    ComplianceGate, ComplianceDecision, dissect_url, ip_is_forbidden,
    host_ssrf_reason, ALLOW, DENY, UNKNOWN)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REG = regmod.load()                       # LE VRAI REGISTRY (2B.1)

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"


def _gate(reg=None, ov=None):
    return ComplianceGate(reg or REG, ov or {})


def _copy_reg():
    return copy.deepcopy(REG)


def _source_entry(source_id, base_url, **kw):
    """Entrée minimale complète (copie de la forme espn)."""
    espn = regmod.get_source(REG, "espn")
    src = copy.deepcopy(espn)
    src.update({"source_id": source_id, "name": source_id,
                "base_url": base_url, "status": kw.pop("status", "TESTED"),
                "enabled": kw.pop("enabled", True),
                "capabilities": kw.pop("capabilities", {"score_live": True})})
    src.update(kw)
    return src


def _reg_with_localtest(host="127.0.0.1", port=8080, scheme="http",
                        source_id="localtest"):
    reg = _copy_reg()
    reg["sources"].append(_source_entry(
        source_id, f"{scheme}://{host}:{port}",
        capabilities={"score_live": True, "standings": True,
                      "historical_results": True, "weather": True,
                      "match_calendar": True}))
    return reg


# ===========================================================================
# A. STATUTS / POLITIQUES — le vrai registry pilote la décision
# ===========================================================================
def test_allow_espn_score_live():
    d = _gate().check_source_access("espn", ESPN_URL, "score_live")
    assert d.decision == ALLOW
    assert "CAPABILITY_OK" in d.reasons and "COMPLIANCE_OK" in d.reasons


def test_allow_espn_capability_partielle():
    d = _gate().check_source_access("espn", ESPN_URL, "lineups")  # 'partial'
    assert d.decision == ALLOW


def test_deny_source_inconnue():
    d = _gate().check_source_access("nimportequoi", ESPN_URL, "score_live")
    assert d.decision == DENY and d.reasons == ("INVALID_SOURCE",)


def test_deny_sofascore_reel_politique_avant_statut():
    d = _gate().check_source_access(
        "sofascore", "https://www.sofascore.com/api/v1/x", "score_live")
    assert d.decision == DENY and "POLICY_NEVER_CONTACT" in d.reasons


def test_deny_flashscore_reel_politique_avant_statut():
    d = _gate().check_source_access(
        "flashscore", "https://www.flashscore.com/x", "score_live")
    assert d.decision == DENY and "POLICY_NEVER_CONTACT" in d.reasons


@pytest.mark.parametrize("blocked", ["sofascore", "flashscore", "fotmob",
                                     "whoscored"])
def test_never_contact_meme_si_registry_trafique(blocked):
    """Même « TESTED + enabled » dans un registry falsifié, la politique
    interne (WEB-0) reste un DENY absolu — avant tout test d'URL."""
    reg = _copy_reg()
    reg["sources"] = [s for s in reg["sources"]
                      if s["source_id"] != blocked]
    reg["sources"].append(_source_entry(blocked, "https://api.never.invalid"))
    d = _gate(reg).check_source_access(blocked, "https://api.never.invalid/x",
                                       "score_live")
    assert d.decision == DENY
    assert "POLICY_NEVER_CONTACT" in d.reasons


def test_deny_sportmonks_disabled():
    d = _gate().check_source_access(
        "sportmonks", "https://api.sportmonks.com/x", "score_live")
    assert d.decision == DENY and "STATUS_DISABLED" in d.reasons


def test_deny_scorebat_deprecated():
    d = _gate().check_source_access(
        "scorebat", "https://www.scorebat.com/video-api/v1/x", "news")
    assert d.decision == DENY and "STATUS_DEPRECATED" in d.reasons


def test_deny_thesportsdb_partial():
    d = _gate().check_source_access(
        "thesportsdb", "https://www.thesportsdb.com/api/v1/json/3/x",
        "match_calendar")
    assert d.decision == DENY and "STATUS_PARTIAL" in d.reasons


def test_deny_api_football_untested():
    d = _gate().check_source_access(
        "api_football", "https://v3.football.api-sports.io/x", "score_live")
    assert d.decision == DENY and "STATUS_UNTESTED" in d.reasons


def test_deny_highlightly_untested():
    d = _gate().check_source_access(
        "highlightly", "https://api.highlightly.net/x", "score_live")
    assert d.decision == DENY and "STATUS_UNTESTED" in d.reasons


def test_deny_enabled_false():
    reg = _copy_reg()
    regmod.get_source(reg, "espn")["enabled"] = False
    d = _gate(reg).check_source_access("espn", ESPN_URL, "score_live")
    assert d.decision == DENY and "SOURCE_DISABLED" in d.reasons


def test_odds_api_reelle_disabled_avant_auth():
    d = _gate().check_source_access(
        "the_odds_api", "https://api.the-odds-api.com/v4/sports", "odds")
    assert d.decision == DENY and "SOURCE_DISABLED" in d.reasons


def test_requires_key_sans_cle_deny():
    reg = _copy_reg()
    regmod.get_source(reg, "the_odds_api")["enabled"] = True
    assert not os.environ.get("PRONOFOOT_KEY_THE_ODDS_API")
    d = _gate(reg).check_source_access(
        "the_odds_api", "https://api.the-odds-api.com/v4/sports", "odds")
    assert d.decision == DENY and "AUTH_KEY_MISSING" in d.reasons


def test_requires_key_avec_cle_env_allow(monkeypatch):
    reg = _copy_reg()
    src = regmod.get_source(reg, "the_odds_api")
    src["enabled"] = True
    src["capabilities"]["odds"] = True
    monkeypatch.setenv("PRONOFOOT_KEY_THE_ODDS_API", "cle-de-test-jetable")
    d = _gate(reg).check_source_access(
        "the_odds_api", "https://api.the-odds-api.com/v4/sports", "odds")
    assert d.decision == ALLOW and "AUTH_KEY_CONFIGURED" in d.reasons
    assert "cle-de-test-jetable" not in str(d.to_dict())     # jamais de fuite


def test_capability_false_deny():
    # registry RÉEL : espn.weather vaut explicitement False (§audit 2B.1)
    d = _gate().check_source_access("espn", ESPN_URL, "weather")
    assert d.decision == DENY and "CAPABILITY_FALSE" in d.reasons


def test_capability_unknown_est_unknown_pas_deny():
    d = _gate().check_source_access("espn", ESPN_URL, "suspensions")
    assert d.decision == UNKNOWN and "CAPABILITY_UNKNOWN" in d.reasons


def test_capability_non_declaree_est_unknown():
    d = _gate().check_source_access("espn", ESPN_URL, "stat_absolument_bidon")
    assert d.decision == UNKNOWN and "CAPABILITY_NOT_DECLARED" in d.reasons


# ===========================================================================
# B. URL — forme, schéma, allowlist (toutes DENY sauf mention contraire)
# ===========================================================================
def test_url_port_443_explicite_allow():
    d = _gate().check_source_access(
        "espn", "https://site.api.espn.com:443/apis/x", "score_live")
    assert d.decision == ALLOW


def test_url_port_inhabituel_deny():
    d = _gate().check_source_access(
        "espn", "https://site.api.espn.com:444/apis/x", "score_live")
    assert d.decision == DENY and "HOST_NOT_ALLOWLISTED" in d.reasons


def test_url_http_refuse():
    d = _gate().check_source_access(
        "espn", "http://site.api.espn.com/apis/x", "score_live")
    assert d.decision == DENY and "SCHEME_HTTP_INTERDIT" in d.reasons


def test_url_hote_evil_deny():
    d = _gate().check_source_access(
        "espn", "https://evil.example.com/apis/x", "score_live")
    assert d.decision == DENY and "HOST_NOT_ALLOWLISTED" in d.reasons


def test_url_userinfo_deny():
    d = _gate().check_source_access(
        "espn", "https://user:pw@site.api.espn.com/apis/x", "score_live")
    assert d.decision == DENY and "USERINFO_INTERDIT" in d.reasons


@pytest.mark.parametrize("url,raison", [
    ("", "URL_VIDE"),
    (None, "URL_TYPE_INVALID"),
    ("javascript:alert(1)", "SCHEME_INTERDIT"),
    ("file:///etc/passwd", "SCHEME_INTERDIT"),
    ("data:text/plain,hello", "SCHEME_INTERDIT"),
    ("ftp://site.api.espn.com/x", "SCHEME_INTERDIT"),
])
def test_url_interdites(url, raison):
    d = _gate().check_source_access("espn", url, "score_live")
    assert d.decision == DENY and raison in d.reasons


def test_url_backslash_deny():
    d = _gate().check_source_access(
        "espn", "https://site.api.espn.com\\@evil.com", "score_live")
    assert d.decision == DENY and "URL_CARACTERES_INTERDITS" in d.reasons


def test_url_espace_deny():
    d = _gate().check_source_access(
        "espn", "https://site.api.espn .com/x", "score_live")
    assert d.decision == DENY and "URL_CARACTERES_INTERDITS" in d.reasons


def test_url_trailing_dot_normalise_allow():
    d = _gate().check_source_access(
        "espn", "https://site.api.espn.com./apis/x", "score_live")
    assert d.decision == ALLOW


def test_url_majuscules_normalisees_allow():
    d = _gate().check_source_access(
        "espn", "HTTPS://SITE.API.ESPN.COM/apis/x", "score_live")
    assert d.decision == ALLOW


def test_decision_deterministe():
    a = _gate().check_source_access("espn", ESPN_URL, "score_live")
    b = _gate().check_source_access("espn", ESPN_URL, "score_live")
    assert a == b and a.decision == b.decision == ALLOW


def test_decision_to_dict_cles():
    d = _gate().check_source_access("espn", ESPN_URL, "score_live").to_dict()
    assert set(d) == {"decision", "source_id", "url", "data_type", "reasons"}


# ===========================================================================
# C. SSRF — littéraux IP/hôtes internes (source localtest dans COPIE registry)
# ===========================================================================
@pytest.mark.parametrize("host,url_host", [
    ("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "0.0.0.0"),
    ("10.0.0.1", "10.0.0.1"), ("172.16.0.1", "172.16.0.1"),
    ("192.168.1.1", "192.168.1.1"), ("169.254.169.254", "169.254.169.254"),
    ("2130706433", "2130706433"), ("0x7f000001", "0x7f000001"),
    ("[::1]", "[::1]"),
])
def test_ssrf_litteraux_ip_deny(host, url_host):
    reg = _reg_with_localtest(host=host)
    g = _gate(reg, {"localtest": {"allow_http": True}})   # http OK, pas privé
    d = g.check_source_access("localtest", f"http://{url_host}:8080/x",
                              "score_live")
    assert d.decision == DENY
    assert any(r.startswith("SSRF") for r in d.reasons), d.reasons


def test_ssrf_localhost_metadata_host():
    reg = _reg_with_localtest(host="localhost")
    g = _gate(reg, {"localtest": {"allow_http": True}})
    d = g.check_source_access("localtest", "http://localhost:8080/x",
                              "score_live")
    assert d.decision == DENY and "SSRF_METADATA_HOST" in d.reasons


def test_ssrf_metadata_google_internal():
    reg = _reg_with_localtest(host="metadata.google.internal")
    g = _gate(reg, {"localtest": {"allow_http": True}})
    d = g.check_source_access(
        "localtest", "http://metadata.google.internal:8080/computeMetadata/v1/",
        "score_live")
    assert d.decision == DENY and "SSRF_METADATA_HOST" in d.reasons


def test_localtest_sans_aucun_override_http_refuse():
    reg = _reg_with_localtest()
    d = _gate(reg).check_source_access(
        "localtest", "http://127.0.0.1:8080/x", "score_live")
    assert d.decision == DENY and "SCHEME_HTTP_INTERDIT" in d.reasons


def test_localtest_overrides_complets_allow():
    """La SEULE porte pour le banc de test : les DEUX overrides explicites."""
    reg = _reg_with_localtest()
    g = _gate(reg, {"localtest": {"allow_http": True,
                                  "allow_private_hosts": True}})
    d = g.check_source_access("localtest", "http://127.0.0.1:8080/x",
                              "score_live")
    assert d.decision == ALLOW


# ===========================================================================
# D. Unitaires — allowlist / dissect_url / ip / pilotage registry / scans
# ===========================================================================
def test_allowed_endpoints_espn_exact():
    eps = _gate().allowed_endpoints("espn")
    assert eps == {("https", "site.api.espn.com", 443)}


def test_allowed_endpoints_domaine_racine_twin():
    reg = _copy_reg()
    reg["sources"].append(_source_entry("racine", "https://example.fr/api"))
    eps = _gate(reg).allowed_endpoints("racine")
    assert eps == {("https", "example.fr", 443), ("https", "www.example.fr", 443)}


def test_allowed_endpoints_www_twin():
    reg = _copy_reg()
    reg["sources"].append(_source_entry("wwwcase", "https://www.example.fr"))
    eps = _gate(reg).allowed_endpoints("wwwcase")
    assert ("https", "example.fr", 443) in eps
    assert ("https", "www.example.fr", 443) in eps


def test_dissect_url_defaut_443():
    p = dissect_url("https://example.com/x?y=1")
    assert p["effective_port"] == 443 and p["port"] is None
    assert p["scheme"] == "https" and p["host"] == "example.com"


def test_dissect_url_port_effectif_explicite():
    p = dissect_url("http://example.com:8080/x")
    assert p["effective_port"] == 8080 and p["scheme"] == "http"


def test_dissect_url_path_query():
    p = dissect_url("https://example.com/a/b?c=d&e=f")
    assert p["path_query"] == "/a/b?c=d&e=f"


def test_ip_is_forbidden_table():
    assert ip_is_forbidden("8.8.8.8") is False
    for bad in ("127.0.0.1", "10.1.2.3", "172.16.5.5", "192.168.0.9",
                "169.254.169.254", "224.0.0.1", "::1", "0.0.0.0", "illisible"):
        assert ip_is_forbidden(bad) is True, bad


def test_host_ssrf_reason_none_pour_domaine_public():
    assert host_ssrf_reason("site.api.espn.com") is None
    assert host_ssrf_reason("www.football-data.co.uk") is None


def test_registry_reelement_pilotee_desactive_puis_allow():
    reg = _copy_reg()
    regmod.get_source(reg, "espn")["enabled"] = False
    assert _gate(reg).check_source_access(
        "espn", ESPN_URL, "score_live").decision == DENY
    regmod.get_source(reg, "espn")["enabled"] = True
    assert _gate(reg).check_source_access(
        "espn", ESPN_URL, "score_live").decision == ALLOW


def test_scan_statique_aucun_bypass_ni_secret():
    """Aucune technique de contournement / aucun secret dans le code WEB-2."""
    tokens = ("_create_unverified_context", "CERT_NONE", "Googlebot",
              "Chrome/", "x-fsign", "cf_clearance", "requests.get(",
              "httpx", "urlopen(", "ghp_", "socks5", "TOR")
    for rel in ("sources/web/compliance.py", "sources/web/safe_http.py",
                "sources/web/response.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for tok in tokens:
            assert tok not in src, f"{rel} contient {tok!r}"


def test_aucun_import_socle_2A_dans_gate():
    src = open(os.path.join(ROOT, "sources/web/compliance.py"),
               encoding="utf-8").read()
    for bad in ("import db", "import app", "import engine ",
                "import repository", "import prediction_service"):
        assert bad not in src
