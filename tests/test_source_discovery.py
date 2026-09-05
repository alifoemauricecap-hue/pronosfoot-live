# -*- coding: utf-8 -*-
"""
TESTS — SOURCE DISCOVERY (ÉTAPE 2B.WEB-1 §17-§20, §29-§32)
==========================================================
Vérifie que la découverte de sources lit le REGISTRY RÉEL 2B.1 (jamais une
copie codée en dur), que les statuts interdits ne sont JAMAIS sélectionnés,
et que tout fonctionne HORS-LIGNE (socket bloqué — §32).
"""
import copy
import json
import os
import socket
import tempfile

import pytest

from sources import registry as regmod
from sources.web import source_discovery as sd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

REG = regmod.load()      # le VRAI registry du projet, validé au chargement


# ---------------------------------------------------------------------------
# §32 — GARDE ANTI-RÉSEAU (autouse)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    calls = {"n": 0}

    class _BlockedSocket(socket.socket):
        def __init__(self, *a, **kw):
            calls["n"] += 1
            raise AssertionError("RÉSEAU INTERDIT pendant les tests (§32)")

    monkeypatch.setattr(socket, "socket", _BlockedSocket)
    yield
    assert calls["n"] == 0


def _reg_copy(**mut):
    """Copie profonde du registry réel + mutations déclaratives."""
    reg = copy.deepcopy(REG)
    return reg


@pytest.fixture()
def discovery():
    return sd.SourceDiscovery(REG)


# ===========================================================================
# §29 — DÉCOUVERTE PAR TYPE (registry réel)
# ===========================================================================
def test_xg_statsbomb(discovery):
    assert discovery.discover("xg") == ["statsbomb_open"]


def test_weather_open_meteo(discovery):
    assert discovery.discover("weather") == ["open_meteo"]


def test_score_live_espn(discovery):
    s = discovery.discover("score_live")
    assert s[0] == "espn"                        # priorités registry respectées
    assert "espn" in s


def test_historical_results_sources_autorisees(discovery):
    s = discovery.discover("historical_results")
    assert s[0] == "football_data_co_uk"         # priorité registry
    assert "espn" in s and "statsbomb_open" in s


def test_odds_sans_cle_the_odds_api_exclu(discovery):
    s = discovery.discover("odds")
    assert "the_odds_api" not in s               # REQUIRES_KEY + enabled=false
    assert "football_data_co_uk" in s and "espn" in s


def test_lineups_api_football_exclu(discovery):
    s = discovery.discover("lineups")
    assert "api_football" not in s               # UNTESTED + enabled=false
    assert s[0] == "espn"


def test_discover_sources_api_fonctionnelle():
    assert sd.discover_sources("xg", REG) == ["statsbomb_open"]
    assert sd.discover_sources("weather", REG) == ["open_meteo"]


# ===========================================================================
# §18/§19/§29 — STATUTS INTERDITS JAMAIS SÉLECTIONNÉS
# ===========================================================================
@pytest.mark.parametrize("sid", ["sofascore", "flashscore", "fotmob", "whoscored"])
def test_never_contact_policy(discovery, sid):
    # §19 : même si un jour le registry les marquait enabled, la politique
    # NEVER_CONTACT les écarte AVANT toute collecte (ici : sans réseau).
    reg = _reg_copy()
    if not regmod.get_source(reg, sid):
        sid_source = copy.deepcopy(regmod.get_source(reg, "sofascore"))
        sid_source["source_id"] = sid
        sid_source["status"] = "TESTED"
        sid_source["enabled"] = True
        reg["sources"].append(sid_source)
        reg["priorities"]["xg"] = [sid, "statsbomb_open"]
    else:
        for s in reg["sources"]:
            if s["source_id"] == sid:
                s["status"] = "TESTED"
                s["enabled"] = True
        reg["priorities"]["xg"] = [sid, "statsbomb_open"]
    d = sd.SourceDiscovery(reg)
    sel = d.discover("xg")
    assert sid not in sel
    assert "statsbomb_open" in sel


def test_status_blocked_jamais_selectionne(discovery):
    for dtype in ("xg", "odds", "score_live", "lineups", "h2h", "form"):
        sel = discovery.discover(dtype)
        assert "sofascore" not in sel and "fbref" not in sel
        assert "understat" not in sel and "bsd" not in sel


def test_status_deprecated_jamais_selectionne(discovery):
    for dtype in ("score_live", "match_calendar", "news", "events"):
        assert "scorebat" not in discovery.discover(dtype)


def test_status_untested_jamais_selectionne(discovery):
    for dtype in ("lineups", "injuries", "player_stats", "score_live"):
        sel = discovery.discover(dtype)
        assert "api_football" not in sel and "highlightly" not in sel


def test_status_requires_key_jamais_selectionne(discovery):
    for dtype in ("odds", "odds_movement", "match_calendar", "standings"):
        sel = discovery.discover(dtype)
        assert "the_odds_api" not in sel and "football_data_org" not in sel


def test_status_disabled_jamais_selectionne(discovery):
    for dtype in ("score_live", "standings", "odds", "lineups"):
        assert "sportmonks" not in discovery.discover(dtype)


def test_explain_honnete_jamais_silencieux(discovery):
    rep = discovery.explain("xg")
    assert rep["selected"] == ["statsbomb_open"]
    rejected_ids = {r["source_id"] for r in rep["rejected"]}
    assert "statsbomb_open" not in rejected_ids
    esp = next(r for r in rep["rejected"] if r["source_id"] == "espn")
    assert esp["capability"] is False or "CAPABILITY_FALSE" in esp["reasons"]


def test_unknown_nest_pas_false_dans_discovery(discovery):
    # §8 : sofascore.xg = unknown → ni sélectionné ni déclaré faux
    rep = discovery.explain("xg")
    sofa = next(r for r in rep["rejected"] if r["source_id"] == "sofascore")
    assert sofa["capability"] == "unknown"
    assert "CAPABILITY_UNKNOWN" in sofa["reasons"]
    assert "sofascore" in discovery.unknown_capability_sources("xg")


def test_unknown_inclus_uniquement_si_demande(discovery):
    reg = _reg_copy()
    reg["priorities"]["test_type"] = ["espn"]
    for s in reg["sources"]:
        if s["source_id"] == "espn":
            s["capabilities"]["test_type"] = "unknown"
    d = sd.SourceDiscovery(reg)
    assert d.discover("test_type") == []
    assert d.discover("test_type", include_unknown=True) == ["espn"]


def test_capability_false_jamais_selectionnee(discovery):
    reg = _reg_copy()
    reg["priorities"]["test_false"] = ["espn", "statsbomb_open"]
    for s in reg["sources"]:
        if s["source_id"] == "espn":
            s["capabilities"]["test_false"] = False
        if s["source_id"] == "statsbomb_open":
            s["capabilities"]["test_false"] = True
    d = sd.SourceDiscovery(reg)
    assert d.discover("test_false") == ["statsbomb_open"]


# ===========================================================================
# §30 — LE MOTEUR UTILISE LE VRAI REGISTRY (pas une copie figée)
# ===========================================================================
def test_registry_reel_utilise_priorite_modifiee(tmp_path):
    reg = _reg_copy()
    rep = sd.SourceDiscovery(reg).explain("historical_results")
    before = sd.SourceDiscovery(reg).discover("historical_results")
    reg["priorities"]["historical_results"] = list(
        reversed(reg["priorities"]["historical_results"]))
    after = sd.SourceDiscovery(reg).discover("historical_results")
    assert after[0] == before[-1]                 # l'ordre suit le JSON


def test_registry_fichier_temporaire_priorite_modifiee(tmp_path):
    reg = _reg_copy()
    # historical_results : openligadb a capability=True → l'ordre inversé l'y met en tête
    chain0 = list(reg["priorities"]["historical_results"])
    reg["priorities"]["historical_results"] = list(reversed(chain0))
    p = tmp_path / "registry_tmp.json"
    p.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    reg2 = regmod.load(str(p))                    # reload depuis le fichier
    d = sd.SourceDiscovery(reg2)
    assert d.discover("historical_results")[0] == chain0[-1]


def test_desactiver_source_disparait(tmp_path):
    reg = _reg_copy()
    for s in reg["sources"]:
        if s["source_id"] == "espn":
            s["enabled"] = False
    d = sd.SourceDiscovery(reg)
    sel = d.discover("score_live")
    assert "espn" not in sel
    # honnêteté §8 : openligadb.score_live = unknown → NON sélectionné par défaut
    assert "openligadb" not in sel
    # …mais jamais déclaré faux : visible explicitement en unknown
    assert "openligadb" in d.unknown_capability_sources("score_live")
    assert d.discover("score_live", include_unknown=True) == ["openligadb"]


def test_reactiver_source_revient(tmp_path):
    reg = _reg_copy()
    # désactiver puis réactiver : l'état final doit être identique au réel
    for s in reg["sources"]:
        if s["source_id"] == "espn":
            s["enabled"] = False
    for s in reg["sources"]:
        if s["source_id"] == "espn":
            s["enabled"] = True
    d = sd.SourceDiscovery(reg)
    assert d.discover("score_live")[0] == "espn"


def test_validation_registry_rejete_enabled_avec_statut_interdit(tmp_path):
    reg = _reg_copy()
    for s in reg["sources"]:
        if s["source_id"] == "sofascore":
            s["enabled"] = True                 # interdit par la validation 2B.1
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(regmod.RegistryError):
        regmod.load(str(p))


def test_discovery_ne_modifie_pas_le_registry(discovery):
    before = json.dumps(REG, sort_keys=True)
    discovery.discover("xg")
    discovery.explain("odds")
    discovery.unknown_capability_sources("xg")
    assert json.dumps(REG, sort_keys=True) == before


# ===========================================================================
# §20 — SOURCES VALIDÉES RECONNUES + WIKIDATA ABSENT DU REGISTRY (honestét)
# ===========================================================================
def test_sources_validees_presentes(discovery):
    valides = {"espn", "football_data_co_uk", "statsbomb_open",
               "open_meteo", "openligadb"}
    ids = {s["source_id"] for s in REG["sources"]}
    assert valides <= ids
    for sid in valides:
        src = regmod.get_source(REG, sid)
        assert src["enabled"] is True and src["status"] == "TESTED"


def test_wikidata_pas_encore_dans_registry(discovery):
    # Honnêteté : wikidata a été testé (2B.WEB-0) mais n'est PAS encore
    # enregistré dans registry.json — l'audit l'avait proposé, la validation
    # humaine décidera. Le moteur ne prétend PAS qu'elle existe côté registry.
    ids = {s["source_id"] for s in REG["sources"]}
    assert "wikidata" not in ids                  # UNKNOWN ≠ FALSE documenté


def test_discovery_donnees_type_inconnu_list_vide(discovery):
    assert discovery.discover("type_inexistant_xyz") == []
    rep = discovery.explain("type_inexistant_xyz")
    assert rep["selected"] == []


def test_determinisme_decouverte(discovery):
    a = discovery.discover("historical_results")
    b = discovery.discover("historical_results")
    assert a == b and len(a) > 0
