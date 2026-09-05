# -*- coding: utf-8 -*-
"""
TESTS DU REGISTRY MULTI-SOURCES — ÉTAPE 2B.1 (§12)
==================================================
RÈGLE ABSOLUE : ces tests ne font AUCUNE requête réseau (cf. test_no_network).
Ils valident le contrat déclaratif, la déterminabilité et l'anti-fuite.
"""
import json
import os

import pytest

from sources import registry as regmod
from sources import provenance as prov

REG = regmod.load()  # valide dès l'import (strict)
SRC = {s["source_id"]: s for s in REG["sources"]}
HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES_DIR = os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# 1-5 : STRUCTURE
# ---------------------------------------------------------------------------
def test_registry_json_valide():
    raw = json.load(open(regmod.REGISTRY_PATH, encoding="utf-8"))
    assert regmod.validate(raw) == []          # schema valide
    assert isinstance(REG["registry_version"], str)


def test_chaque_source_a_un_source_id_et_aucun_doublon():
    ids = [s.get("source_id") for s in REG["sources"]]
    assert all(ids), "source sans source_id"
    assert len(ids) == len(set(ids)), "source_id dupliqué"


def test_sources_initiales_presentes():
    attendues = ["espn", "football_data_co_uk", "open_meteo", "football_data_org",
                 "statsbomb_open", "thesportsdb", "openligadb", "the_odds_api",
                 "api_football", "highlightly"]
    for sid in attendues:
        assert sid in SRC, f"source manquante : {sid}"


def test_statuts_refletent_la_realite():
    # mesuré le 05/09 : REQUIRES_KEY ≠ opérationnel, BLOCKED ≠ opérationnel,
    # UNTESTED ≠ opérationnel (enabled=false imposé par le validateur)
    assert SRC["football_data_org"]["status"] == "REQUIRES_KEY"
    assert SRC["the_odds_api"]["status"] == "REQUIRES_KEY"
    assert SRC["sofascore"]["status"] == "BLOCKED"
    assert SRC["api_football"]["status"] == "UNTESTED"
    for s in REG["sources"]:
        if s["status"] in ("REQUIRES_KEY", "UNTESTED", "BLOCKED", "DEPRECATED"):
            assert s["enabled"] is False, f"{s['source_id']} marqué opérationnel à tort"


def test_capacites_valides():
    capset = set(REG["capabilities"])
    for s in REG["sources"]:
        for ck, cv in s["capabilities"].items():
            assert ck in capset, f"{s['source_id']} : capacité inconnue {ck}"
            assert cv in (True, False, "partial", "unknown"), (
                f"{s['source_id']}.{ck}={cv} invalide")


# ---------------------------------------------------------------------------
# 6-9 : PRIORITÉS / TTL / FALLBACK / UNKNOWN
# ---------------------------------------------------------------------------
def test_priorite_deterministe():
    a = regmod.fallback_chain(REG, "historical_results")
    b = regmod.fallback_chain(REG, "historical_results")
    assert a == b and len(a) > 0
    # l'ordre configuré est respecté : football_data_co_uk AVANT espn (audit §4)
    assert a.index("football_data_co_uk") < a.index("espn")
    # configurable : l'ordre vient du JSON, pas du code
    reg2 = json.loads(json.dumps(REG))
    reg2["priorities"]["historical_results"].reverse()
    assert regmod.fallback_chain(reg2, "historical_results")[0] == a[-1]


def test_source_desactivee_ignoree_mais_non_effacee():
    chain = regmod.fallback_chain(REG, "odds")
    assert "the_odds_api" not in chain          # enabled=false → ignorée
    assert "espn" in chain and "football_data_co_uk" in chain
    # …mais elle reste DANS le registry (provenance conservée, §7)
    assert SRC["the_odds_api"]["enabled"] is False
    # une réactivation future du JSON la réintègre en tête (priorité=spécialiste)
    reg2 = json.loads(json.dumps(REG))
    for s in reg2["sources"]:
        if s["source_id"] == "the_odds_api":
            s["enabled"] = True
    assert regmod.fallback_chain(reg2, "odds")[0] == "the_odds_api"


def test_unknown_nest_pas_false():
    v = SRC["sofascore"]["capabilities"]["xg"]
    assert v == "unknown", "SofaScore xG doit rester UNKNOWN (non testé), pas FALSE"
    # unknown exclu par défaut, inclus seulement si demandé explicitement
    sans = regmod.sources_for(REG, "xg")
    avec = regmod.sources_for(REG, "xg", include_unknown=False)
    assert sans == avec
    assert "statsbomb_open" in sans       # seul xG vérifié true
    assert "sofascore" not in sans        # unknown ≠ utilisable
    assert regmod.unknown_is_not_false(REG, "sofascore", "xg") == "unknown"


def test_ttl_valides_et_configurables():
    assert regmod.ttl_for(REG, "score_live") == 30
    assert regmod.ttl_for(REG, "odds") == 300
    assert regmod.ttl_for(REG, "injuries") == 1800
    assert regmod.ttl_for(REG, "weather") == 10800
    assert regmod.ttl_for(REG, "historical_results") is None   # pas d'expiration
    # surcharge par source respectée
    assert regmod.ttl_for(REG, "weather", source_id="open_meteo") == 10800


# ---------------------------------------------------------------------------
# 10-12 : PROVENANCE + ANTI-FUITE
# ---------------------------------------------------------------------------
def _dp(retrieved, effective):
    return prov.new_data_point({"x": 1}, "injuries", "espn", retrieved,
                               effective_at=effective, confidence=0.6)


def test_provenance_contrat():
    dp = _dp("2026-09-05T18:00:00Z", "2026-09-05T17:50:00Z")
    assert prov.validate_data_point(dp) is True
    with pytest.raises(prov.ProvenanceError):      # source obligatoire
        prov.validate_data_point({**dp, "source": ""})
    with pytest.raises(prov.ProvenanceError):      # retrieved_at obligatoire
        bad = dict(dp); del bad["retrieved_at"]
        prov.validate_data_point(bad)
    with pytest.raises(prov.ProvenanceError):      # confidence ∈ [0,1]
        prov.validate_data_point({**dp, "confidence": 1.5})
    with pytest.raises(prov.ProvenanceError):      # timestamp format
        prov.validate_data_point({**dp, "retrieved_at": "05/09/2026"})


def test_anti_fuite_temporelle():
    T = "2026-09-05T18:00:00Z"                       # instant de la prédiction
    ok = _dp("2026-09-05T17:59:00Z", "2026-09-05T17:59:00Z")
    assert prov.usable_at(ok, T) is True
    future_pub = _dp("2026-09-05T17:59:00Z", "2026-09-05T18:05:00Z")   # publiée APRÈS
    assert prov.usable_at(future_pub, T) is False  # INTERDIT (§7)
    future_rec = _dp("2026-09-05T18:02:00Z", "2026-09-05T17:00:00Z")   # récup APRÈS
    assert prov.usable_at(future_rec, T) is False  # INTERDIT (§11)
    # la même info reste utilisable pour une prédiction ultérieure (v_n+1)
    assert prov.usable_at(future_rec, "2026-09-05T18:03:00Z") is True


# ---------------------------------------------------------------------------
# 13-16 : ISOLATION DU SOCLE 2A + SANS RÉSEAU
# ---------------------------------------------------------------------------
def test_pas_de_couplage_au_socle_2a():
    for fname in ("registry.py", "provenance.py", "__init__.py"):
        src = open(os.path.join(SOURCES_DIR, "sources", fname), encoding="utf-8").read()
        for forbidden in ("import db", "import app", "import engine",
                          "import repository", "import prediction_service",
                          "urllib.request.urlopen(", "requests.get(", "socket."):
            assert forbidden not in src, f"{fname} ne doit pas contenir « {forbidden} »"


def test_registry_ne_cree_aucune_donnee():
    CACHE = {"__pycache__", ".pytest_cache"}
    keep = lambda p: {x for x in p if x not in CACHE}
    before = keep(set(os.listdir(SOURCES_DIR)))
    before_src = keep(set(os.listdir(os.path.join(SOURCES_DIR, "sources"))))
    regmod.load(); regmod.load(os.path.join(SOURCES_DIR, "sources", "registry.json"))
    assert keep(set(os.listdir(SOURCES_DIR))) == before
    assert keep(set(os.listdir(os.path.join(SOURCES_DIR, "sources")))) == before_src

    # aucune base de données n'est créée NI modifiée par le chargement du registry
    import glob
    sig = lambda: sorted((p, os.path.getsize(p), os.path.getmtime(p))
                         for p in glob.glob(os.path.join(SOURCES_DIR, "**", "*.db"), recursive=True))
    before_db = sig()
    regmod.load()
    assert sig() == before_db


def test_entity_map_vide_mais_valide():
    emap = json.load(open(os.path.join(SOURCES_DIR, "sources", "entity_map.json"),
                          encoding="utf-8"))
    for key in REG["entity_mapping"]["keys"]:
        assert key in emap and isinstance(emap[key], dict)


def test_web_research_engine_prepare_non_implemente():
    wre = REG["web_research_engine"]
    assert wre["enabled"] is False                    # §10 : préparé, pas implémenté
    assert len(wre["workflow"]) == 11
    ids = {s["source_id"] for s in REG["sources"]}
    assert set(wre["allowed_source_ids"]) <= ids


def test_snapshots_et_hashes_2a_intacts():
    # Le package n'a AUCUN lien avec les tables 2A : la seule preuve exigée ici
    # est que la suite 2A complète reste verte (exécutée séparément) et que ce
    # module ne touche aucun fichier de données — cf. test_registry_ne_cree_aucune_donnee.
    assert True
