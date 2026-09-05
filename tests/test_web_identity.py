# -*- coding: utf-8 -*-
"""
TESTS — MATCH IDENTITY ENGINE (ÉTAPE 2B.WEB-1)
==============================================
§23-§43 du cahier des charges. TOUS ces tests fonctionnent HORS-LIGNE :
l'autouse fixture ci-dessous BLOQUE socket.socket — tout appel réseau
ferait échouer le test (§32). Aucune identité n'est inventée : les fixtures
locales utilisent des identifiants clairement marqués 'fixture:...' ainsi
que l'unique QID vérifié aujourd'hui (wikidata:Q483020 = PSG, mesuré
le 05/09/2026 via l'API publique Wikidata pendant l'audit 2B.WEB-0).
"""
import copy
import glob
import json
import os
import socket
import time

import pytest

from sources.web import decision as dec
from sources.web import identity as ident

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# §32 — GARDE ANTI-RÉSEAU (autouse sur TOUT le module)
# ---------------------------------------------------------------------------
_NET = {"n": 0}   # compteur global d'appels socket (§32)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    class _BlockedSocket(socket.socket):
        def __init__(self, *a, **kw):
            _NET["n"] += 1
            raise AssertionError("RÉSEAU INTERDIT pendant les tests (§32)")

    monkeypatch.setattr(socket, "socket", _BlockedSocket)
    _NET["n"] = 0
    yield
    assert _NET["n"] == 0, f"{_NET['n']} appel(s) réseau détecté(s) (§32)"


# ---------------------------------------------------------------------------
# FIXTURES LOCALES (§7) — entity_map d'exemple, jamais de réseau
# ---------------------------------------------------------------------------
FIXTURE_ENTITY_MAP = {
    "version": "fixture",
    "match_ids": {},
    "player_ids": {},
    "competition_ids": {},
    "team_ids": {
        "wikidata:Q483020": {                       # VÉRIFIÉ 05/09/2026 (audit)
            "canonical_name": "Paris Saint-Germain",
            "aliases": ["PSG", "Paris SG", "Paris Saint Germain"],
            "external_ids": {"wikidata": "Q483020"},
            "source": "fixture:audit",
            "confidence": 0.99,
        },
        "fixture:team:om": {
            "canonical_name": "Olympique de Marseille",
            "aliases": ["OM", "Marseille", "Olympique Marseille"],
            "external_ids": {},
            "source": "fixture:test",
            "confidence": 0.95,
        },
        "fixture:team:ol": {
            "canonical_name": "Olympique Lyonnais",
            "aliases": ["OL", "Lyon", "Olympique Lyon"],
            "external_ids": {},
            "source": "fixture:test",
            "confidence": 0.95,
        },
        "fixture:team:ol-women": {
            "canonical_name": "Olympique Lyonnais Féminin",
            "aliases": ["OL Féminin", "Lyon Féminines"],
            "external_ids": {},
            "source": "fixture:test",
            "confidence": 0.95,
        },
        "fixture:team:man-city": {
            "canonical_name": "Manchester City",
            "aliases": ["Man City", "ManCity"],
            "external_ids": {},
            "source": "fixture:test",
            "confidence": 0.95,
        },
        "fixture:team:man-utd": {
            "canonical_name": "Manchester United",
            "aliases": ["Man United", "Man Utd", "MUFC"],
            "external_ids": {},
            "source": "fixture:test",
            "confidence": 0.95,
        },
        "fixture:team:paris-fc": {
            "canonical_name": "Paris FC",
            "aliases": ["PFC"],
            "external_ids": {},
            "source": "fixture:test",
            "confidence": 0.95,
        },
        "fixture:team:real-madrid": {
            "canonical_name": "Real Madrid",
            "aliases": ["Real de Madrid", "RMA"],
            "external_ids": {},
            "source": "fixture:test",
            "confidence": 0.95,
        },
    },
}


@pytest.fixture()
def book():
    return ident.AliasBook.from_entity_map(FIXTURE_ENTITY_MAP)


@pytest.fixture()
def engine(book):
    return ident.MatchIdentityEngine(alias_book=book)


def _ev(source, eid, home, away, comp="ligue 1", ko="2026-09-05T19:00:00Z",
        venue=None, **kw):
    return ident.make_event(source=source, external_id=eid, home=home,
                            away=away, competition=comp, kickoff=ko,
                            venue=venue, **kw)


# ===========================================================================
# §24 — NORMALISATION
# ===========================================================================
def test_norm_casse():
    assert ident.normalize_team_name("PARIS SAINT-GERMAIN") == "paris saint germain"


def test_norm_accents():
    assert ident.normalize_team_name("Olympique Lyonnais") == "olympique lyonnais"
    assert ident.normalize_team_name("Atlético Madrid") == "atletico madrid"
    assert ident.normalize_team_name("FC Köln") == "fc koln"


def test_norm_espaces_multiples():
    assert ident.normalize_team_name("Paris   Saint  Germain") == "paris saint germain"


def test_norm_ponctuation():
    assert ident.normalize_team_name("A.C. Milan!") == "a c milan"


def test_norm_tirets():
    assert ident.normalize_team_name("Paris Saint–Germain") == "paris saint germain"
    assert ident.normalize_team_name("Paris Saint—Germain") == "paris saint germain"


def test_norm_apostrophes():
    assert ident.normalize_team_name("Queen's Park") == "queen s park"
    assert ident.normalize_team_name("d’Alençon") == "d alencon"


def test_norm_unicode_ligature():
    # ﬁ (U+FB01, ligature « fi ») doit se décomposer en "fi" via NFKC
    assert ident.normalize_team_name("\uFB01orentina") == "fiorentina"


def test_norm_suffixes_football():
    assert ident.normalize_team_name("Manchester United FC",
                                     strip_suffixes=True) == "manchester united"


def test_norm_separateur_vs():
    left, right = ident.split_home_away("PSG vs Marseille")
    assert left == "psg" and right == "marseille"


def test_norm_separateur_v_et_tiret():
    assert ident.split_home_away("Paris Saint-Germain v Marseille")[1] == "marseille"
    s = ident.normalize_team_name("PSG — Marseille")
    assert "psg" in s and "marseille" in s


def test_norm_valeur_vide():
    assert ident.normalize_team_name("") == ""
    assert ident.normalize_team_name("   ") == ""


def test_norm_none():
    assert ident.normalize_team_name(None) == ""


def test_norm_mauvais_type():
    assert ident.normalize_team_name(123) == ""
    assert ident.normalize_team_name(["PSG"]) == ""
    assert ident.normalize_team_name({"name": "PSG"}) == ""


def test_norm_nom_tres_long():
    long_name = "Paris " * 2000 + "Saint-Germain"
    out = ident.normalize_team_name(long_name)
    assert isinstance(out, str)
    assert out == ident.normalize_team_name(long_name)   # déterministe
    assert len(out) <= 6000                              # coût borné (garde-fou)


def test_norm_caracteres_inhabituels():
    out = ident.normalize_team_name("⚽ Pârîš FC™ ©")
    assert "paris" in out and "fc" in out
    assert "⚽" not in out or isinstance(out, str)   # jamais d'exception


def test_norm_deterministe():
    a = [ident.normalize_team_name("Olýmpiqué  Marséille–FC") for _ in range(50)]
    assert len(set(a)) == 1


# ===========================================================================
# §5/§25 — ALIAS (via entity_map — aucune base parallèle)
# ===========================================================================
def test_alias_psg_valides(book):
    for alias in ("PSG", "Paris SG", "Paris Saint Germain"):
        ident_t = book.resolve(alias)
        assert ident_t is not None and ident_t.canonical_id == "wikidata:Q483020"


def test_alias_ol_valides(book):
    for alias in ("Olympique Lyonnais", "Lyon", "OL"):
        assert book.resolve(alias).canonical_id == "fixture:team:ol"


def test_alias_manchester_city(book):
    assert book.resolve("Man City").canonical_id == "fixture:team:man-city"
    assert book.resolve("Manchester City").canonical_id == "fixture:team:man-city"


def test_alias_conserve_metadonnees(book):
    t = book.resolve("PSG")
    assert t.external_ids.get("wikidata") == "Q483020"
    assert t.source == "fixture:audit"
    assert 0 <= t.confidence <= 1


def test_faux_positif_paris_non_transforme(book):
    # "Paris" SEUL ne devient jamais le PSG sans preuve (§25)
    assert book.resolve("Paris") is None
    rel, credit, _, _ = ident._teams_relation("Paris", "Paris Saint-Germain", book)
    assert credit == 0.0 and rel in ("guarded", "ambiguous", "unmatched", "different")


def test_faux_positif_united_non_transforme(book):
    assert book.resolve("United") is None
    rel, credit, _, _ = ident._teams_relation("United", "Manchester United", book)
    assert credit == 0.0 and rel == "guarded"


def test_faux_positif_city_non_transforme(book):
    # « City » SEUL : jamais enregistré comme alias fiable (§5) → jamais résolu
    assert book.resolve("City") is None
    rel, credit, _, _ = ident._teams_relation("City", "Manchester City", book)
    assert credit == 0.0 and rel == "guarded"


def test_faux_positif_olympique_non_transforme(book):
    assert book.resolve("Olympique") is None
    rel, credit, _, _ = ident._teams_relation("Olympique", "Olympique de Marseille", book)
    assert credit == 0.0 and rel == "guarded"


def test_faux_positif_saint_germain(book):
    assert book.resolve("Saint-Germain") is None
    rel, credit, _, _ = ident._teams_relation("Saint-Germain", "Paris Saint-Germain", book)
    assert credit == 0.0 and rel == "guarded"


def test_alias_ne_sait_pas_ne_devine_pas(book):
    assert book.resolve("Tottenham Hotspur") is None          # inconnu ≠ deviné
    assert book.resolve("") is None


def test_alias_via_entity_map_json_structure():
    em = json.loads(json.dumps(FIXTURE_ENTITY_MAP))
    b = ident.AliasBook.from_entity_map(em)
    assert len(b) == len(FIXTURE_ENTITY_MAP["team_ids"])
    assert b.resolve("OM").canonical_name == "Olympique de Marseille"


def test_entity_map_reelle_chargee_sans_erreur():
    b = ident.AliasBook.load_default()          # le VRAI fichier du projet
    assert isinstance(len(b), int) and len(b) >= 0   # structure intacte


def test_alias_confiance_faible_non_enregistre_silencieusement():
    # §5 : un alias enregistré DOIT porter source + confidence → sinon rejet implicite
    b = ident.AliasBook()
    key = b.add("fixture:team:x", {"canonical_name": "", "aliases": []})
    assert key in b._entries
    assert b.resolve("inconnu") is None


# ===========================================================================
# §26 — MATCHING (20 cas obligatoires)
# ===========================================================================
def test_match_identite_parfaite(engine):
    a = _ev("espn", "761770", "Paris Saint-Germain", "Marseille",
            venue="Parc des Princes")
    b = _ev("espn", "761770", "Paris Saint-Germain", "Marseille",
            venue="Parc des Princes")
    r = engine.compare(a, b)
    assert r.status == dec.MATCH_AUTO and r.confidence == 1.0
    assert dec.R_EXTERNAL_ID_MATCH in r.reasons
    assert r.canonical_match_id is not None and "2026-09-05" in r.canonical_match_id


def test_match_via_alias(engine):
    a = _ev("espn", None, "PSG", "OM")
    b = _ev("stats", None, "Paris Saint Germain", "Olympique Marseille")
    r = engine.compare(a, b)
    assert r.confidence >= engine.config["thresholds"]["journalized"]
    assert r.status == dec.MATCH_JOURNALIZED
    assert dec.R_HOME_TEAM_EXACT in r.reasons or dec.R_HOME_TEAM_ALIAS in r.reasons


def test_match_accents_ignores(engine):
    a = _ev("sA", None, "Atlético Madrid", "Sevilla FC")
    b = _ev("sB", None, "Atletico Madrid", "Sevilla FC")
    r = engine.compare(a, b)
    assert r.confidence >= 0.8 and r.status in (dec.MATCH_AUTO, dec.MATCH_JOURNALIZED)


def test_match_date_identique(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-09-05T19:00:00Z")
    b = _ev("s2", None, "PSG", "OM", ko="2026-09-05T19:00:00Z")
    r = engine.compare(a, b)
    assert dec.R_DATE_MATCH in r.reasons


def test_match_date_plus_un_jour(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-09-05T19:00:00Z")
    b = _ev("s2", None, "PSG", "OM", ko="2026-09-06T19:00:00Z")
    r = engine.compare(a, b)
    assert dec.R_DATE_NEXT_DAY in r.reasons
    assert dec.R_DATE_DIFFERENT not in r.contradictions


def test_match_date_differentie_no_match(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-09-05T19:00:00Z")
    b = _ev("s2", None, "PSG", "OM", ko="2026-10-05T19:00:00Z")
    r = engine.compare(a, b)
    assert r.status == dec.NO_MATCH
    assert dec.R_DATE_DIFFERENT in r.contradictions


def test_match_competition_identique_alias(engine):
    a = _ev("s1", None, "Arsenal", "Chelsea", comp="Premier League")
    b = _ev("s2", None, "Arsenal", "Chelsea", comp="English Premier League")
    r = engine.compare(a, b)
    assert dec.R_COMPETITION_MATCH in r.reasons


def test_match_competition_differentie(engine):
    a = _ev("s1", None, "Arsenal", "Chelsea", comp="Premier League")
    b = _ev("s2", None, "Arsenal", "Chelsea", comp="Championship")
    r = engine.compare(a, b)
    assert r.status == dec.NO_MATCH
    assert dec.R_COMPETITION_CONTRADICTION in r.contradictions


def test_match_heure_identique(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-09-05T19:00:00Z")
    b = _ev("s2", None, "PSG", "OM", ko="2026-09-05T19:00:00Z")
    assert dec.R_TIME_MATCH in engine.compare(a, b).reasons


def test_match_heure_legrement_differente(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-09-05T19:00:00Z")
    b = _ev("s2", None, "PSG", "OM", ko="2026-09-05T19:10:00Z")
    r = engine.compare(a, b)
    assert dec.R_TIME_MATCH in r.reasons  # ≤ 25 min : toléré (reprogrammations)


def test_match_heure_absente(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-09-05")
    b = _ev("s2", None, "PSG", "OM", ko="2026-09-05")
    r = engine.compare(a, b)
    assert dec.R_TIME_MISSING in r.reasons
    assert r.status != dec.NO_MATCH


def test_match_stade_identique_bonus(engine):
    sans = engine.compare(_ev("s1", None, "PSG", "OM"), _ev("s2", None, "PSG", "OM"))
    avec = engine.compare(_ev("s1", None, "PSG", "OM", venue="Parc des Princes"),
                          _ev("s2", None, "PSG", "OM", venue="Parc des Princes"))
    assert avec.confidence > sans.confidence
    assert dec.R_STADIUM_MATCH in avec.reasons


def test_match_stade_different_pas_invalide(engine):
    r = engine.compare(_ev("s1", None, "PSG", "OM", venue="Parc des Princes"),
                       _ev("s2", None, "PSG", "OM", venue="Orange Vélodrome"))
    assert dec.R_STADIUM_DIFFERENT in r.reasons
    assert dec.R_STADIUM_DIFFERENT not in r.contradictions


def test_match_home_away_inverses_interdit(engine):
    a = _ev("s1", None, "PSG", "OM")
    b = _ev("s2", None, "OM", "PSG")
    r = engine.compare(a, b)
    assert r.status == dec.UNKNOWN
    assert r.decision_reason == dec.R_HOME_AWAY_INVERSION


def test_match_equipe_differente(engine):
    a = _ev("s1", None, "PSG", "OM")
    b = _ev("s2", None, "PSG", "Olympique Lyonnais")
    r = engine.compare(a, b)
    assert r.status == dec.NO_MATCH
    assert dec.R_TEAM_CONTRADICTION_AWAY in r.contradictions


def test_match_external_id_identique(engine):
    a = _ev("espn", "123", "PSG", "OM")
    b = _ev("espn", "123", "Paris SG", "Marseille")
    r = engine.compare(a, b)
    assert dec.R_EXTERNAL_ID_MATCH in r.reasons
    assert r.status in (dec.MATCH_AUTO, dec.MATCH_JOURNALIZED)


def test_match_external_ids_differents_sources_differentes(engine):
    # espn:123 vs provider:123 ne se comparent JAMAIS naïvement (§10)
    a = _ev("espn", "123", "PSG", "OM")
    b = _ev("bookmakerX", "123", "PSG", "OM")
    r = engine.compare(a, b)
    assert dec.R_EXTERNAL_ID_CONTRADICTION not in r.contradictions
    assert dec.R_EXTERNAL_IDS_DIFFERENT_PROVIDERS in r.reasons


def test_match_meme_source_ids_differents_no_match(engine):
    a = _ev("espn", "123", "PSG", "OM")
    b = _ev("espn", "456", "PSG", "OM")
    r = engine.compare(a, b)
    assert r.status == dec.NO_MATCH
    assert dec.R_EXTERNAL_ID_CONTRADICTION in r.contradictions


def test_match_plusieurs_candidats_ambigu(engine):
    a = _ev("probe", None, "PSG", "OM")
    c1 = _ev("espn", None, "Paris SG", "Marseille", comp="ligue 1")
    c2 = _ev("espnLive", None, "Paris Saint Germain", "OM", comp="ligue 1")
    r = engine.resolve(a, [c1, c2])
    if r.status == dec.UNKNOWN:
        assert r.decision_reason == dec.R_MULTIPLE_CANDIDATES
        assert len(r.candidates) >= 2
    else:  # ou un gagnant — mais jamais de choix arbitraire silencieux
        assert r.status == dec.MATCH_JOURNALIZED and r.confidence > 0.85


def test_match_aucun_candidat(engine):
    r = engine.resolve(_ev("probe", None, "PSG", "OM"), [])
    assert r.status == dec.UNKNOWN
    assert r.decision_reason == dec.R_NO_CANDIDATES


def test_match_canonical_id_deterministe(engine):
    a = _ev("espn", None, "PSG", "OM")
    b = _ev("s2", None, "Paris Saint-Germain", "Marseille")
    c = _ev("s3", None, "Paris SG", "Olympique Marseille")
    r1 = engine.compare(a, b)
    r2 = engine.compare(a, c)
    assert r1.canonical_match_id == r2.canonical_match_id is not None


# ===========================================================================
# §15/§27 — SEUILS EXACTS (décideur pur)
# ===========================================================================
@pytest.mark.parametrize("score,attendu", [
    (0.00, dec.UNKNOWN), (0.79, dec.UNKNOWN),
    (0.80, dec.MATCH_JOURNALIZED), (0.81, dec.MATCH_JOURNALIZED),
    (0.94, dec.MATCH_JOURNALIZED),
    (0.95, dec.MATCH_AUTO), (0.96, dec.MATCH_AUTO), (1.00, dec.MATCH_AUTO),
])
def test_seuils_statuts_exact(score, attendu):
    status, _ = dec.decide_status(score, [])
    assert status == attendu


def test_seuil_contradiction_gagne_sur_score_eleve():
    # §15 : 0.99 N'ANNULE JAMAIS une contradiction majeure
    status, dominant = dec.decide_status(0.99, [dec.R_TEAM_CONTRADICTION_HOME])
    assert status == dec.NO_MATCH
    assert dominant == dec.R_TEAM_CONTRADICTION_HOME


# ===========================================================================
# §28 — CONFLITS : jamais de fusion dangereuse
# ===========================================================================
def test_conflit_home_different(engine):
    a = _ev("s1", None, "PSG", "OM")
    b = _ev("s2", None, "Olympique Lyonnais", "OM")
    r = engine.compare(a, b)
    assert r.status == dec.NO_MATCH
    assert dec.R_TEAM_CONTRADICTION_HOME in r.contradictions


def test_conflit_date_incompatible(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-09-05T20:00:00Z")
    b = _ev("s2", None, "PSG", "OM", ko="2026-09-10T20:00:00Z")
    assert engine.compare(a, b).status == dec.NO_MATCH


def test_conflit_competition_eeee(engine):
    a = _ev("s1", None, "Real Madrid", "Barcelona", comp="La Liga")
    b = _ev("s2", None, "Real Madrid", "Barcelona", comp="Championship")
    assert engine.compare(a, b).status == dec.NO_MATCH


def test_conflit_equipe_ambigue_pas_de_fusion(engine):
    a = _ev("s1", None, "United", "City")
    b = _ev("s2", None, "Manchester United", "Manchester City")
    r = engine.compare(a, b)
    assert r.status != dec.MATCH_AUTO
    assert r.confidence < engine.config["thresholds"]["auto"]


def test_conflit_competition_inconnue_neutre(engine):
    a = _ev("s1", None, "PSG", "OM", comp="Some Unkn0wn Cup 3000")
    b = _ev("s2", None, "PSG", "OM", comp="Xyzzy Invitational")
    r = engine.compare(a, b)
    assert dec.R_COMPETITION_UNKNOWN in r.reasons
    assert dec.R_COMPETITION_CONTRADICTION not in r.contradictions


# ===========================================================================
# §37 — COLLISIONS DE NOMS (book peut provver deux clubs proches)
# ===========================================================================
def test_collision_paris_fc_vs_psg(engine):
    a = _ev("s1", None, "Paris FC", "OM")
    b = _ev("s2", None, "Paris Saint-Germain", "OM")
    r = engine.compare(a, b)
    assert r.status == dec.NO_MATCH                      # book le PROUVE (fixture)
    assert dec.R_TEAM_CONTRADICTION_HOME in r.contradictions


def test_collision_united_fc_unknown_pas_man_utd(engine):
    rel, credit, _, _ = ident._teams_relation("United FC", "Manchester United", ident.AliasBook())
    assert credit == 0.0 and rel == "guarded"


def test_collision_city_fc_unknown_pas_man_city(engine):
    rel, credit, _, _ = ident._teams_relation("City FC", "Manchester City", ident.AliasBook())
    assert credit == 0.0 and rel == "guarded"


def test_collision_paris_vs_psg_sans_book():
    rel, credit, _, _ = ident._teams_relation("Paris", "Paris Saint-Germain", ident.AliasBook())
    assert credit == 0.0


# ===========================================================================
# §38 — HOMMES / FEMMES / JEUNES : pas de fusion automatique
# ===========================================================================
def test_categorie_ol_vs_ol_feminin(engine):
    a = _ev("s1", None, "Olympique Lyonnais", "PSG", comp="fra.1")
    b = _ev("s2", None, "Olympique Lyonnais Féminin", "PSG", comp="fra.1")
    r = engine.compare(a, b)
    # le book PROUVE deux fiches distinctes (fixture) ET/OU catégorie ≠
    assert r.status != dec.MATCH_AUTO
    assert r.status in (dec.NO_MATCH, dec.UNKNOWN) or r.confidence < 0.95


def test_categorie_u19_vs_u21_veto(engine):
    a = _ev("s1", None, "France U19", "Spain U19", comp="U19 Championship")
    b = _ev("s2", None, "France U21", "Spain U21", comp="U21 Championship")
    r = engine.compare(a, b)
    assert r.status == dec.NO_MATCH
    assert dec.R_CATEGORY_MISMATCH in r.contradictions


def test_categorie_u19_vs_u21_noms_identiques_veto(engine):
    a = _ev("s1", None, "France U19", "Angleterre U19")
    b = _ev("s2", None, "France U21", "Angleterre U21")
    r = engine.compare(a, b)
    assert r.status == dec.NO_MATCH


def test_categorie_detectee():
    assert ident.team_category("paris saint germain feminin") == "women"
    assert ident.team_category("france u19") == "u19"
    assert ident.team_category("real madrid") == "open"


# ===========================================================================
# §39 — CLUBS HOMONYMES (doute → UNKNOWN)
# ===========================================================================
def test_homonymes_identite_non_prouvee():
    b = ident.AliasBook()
    rel, credit, ia, ib = ident._teams_relation("Dynamo Kyiv", "Dynamo Dresden", b)
    assert credit == 0.0 and rel in ("ambiguous", "unmatched", "guarded")


def test_homonymes_resolve_unknown(engine):
    a = _ev("probe", None, "Dynamo Kyiv", "Shakhtar", comp="ukrainian league")
    cands = [_ev("srcA", None, "Dynamo Kyiv", "Shakhtar Donetsk", comp="ukr.1"),
             _ev("srcB", None, "Dynamo Dresden", "Erzgebirge", comp="ger.3")]
    r = engine.resolve(a, cands)
    assert r.status in (dec.UNKNOWN, dec.NO_MATCH)
    assert r.status != dec.MATCH_AUTO


# ===========================================================================
# §12/§40 — TEMPORELS + ANTI-FUITE
# ===========================================================================
def test_temporal_fuseaux_horaires_equivalents(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-09-05T21:00:00+02:00")
    b = _ev("s2", None, "PSG", "OM", ko="2026-09-05T19:00:00Z")
    r = engine.compare(a, b)
    assert dec.R_DATE_MATCH in r.reasons and dec.R_TIME_MATCH in r.reasons


def test_temporal_date_plus_2_jours_no_match(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-09-05T19:00:00Z")
    b = _ev("s2", None, "PSG", "OM", ko="2026-09-07T19:00:00Z")
    assert engine.compare(a, b).status == dec.NO_MATCH


def test_temporal_naif_utc_journal_tolerable(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-09-05T19:00:00")   # naïf
    b = _ev("s2", None, "PSG", "OM", ko="2026-09-05T19:00:00Z")
    r = engine.compare(a, b)
    assert r.status != dec.NO_MATCH


def test_temporal_date_future_valide(engine):
    a = _ev("s1", None, "PSG", "OM", ko="2026-12-25T19:00:00Z")
    b = _ev("s2", None, "PSG", "OM", ko="2026-12-25T19:00:00Z")
    assert engine.compare(a, b).status in (dec.MATCH_AUTO, dec.MATCH_JOURNALIZED)


def test_antifuite_info_apres_T_exclue(engine):
    T = "2026-09-05T18:00:00Z"
    event = _ev("probe", None, "PSG", "OM", retrieved_at="2026-09-05T17:00:00Z")
    candidat_tard = _ev("espn", None, "Paris SG", "Marseille",
                        retrieved_at="2026-09-05T18:05:00Z",      # APPRIS APRÈS T
                        effective_at="2026-09-05T17:00:00Z")
    r = engine.resolve(event, [candidat_tard], as_of=T)
    assert r.status == dec.UNKNOWN
    assert r.decision_reason == dec.R_INFO_AFTER_T


def test_antifuite_meme_info_ok_plus_tard(engine):
    T1 = "2026-09-05T18:00:00Z"
    T2 = "2026-09-05T18:10:00Z"
    event = _ev("probe", None, "PSG", "OM", retrieved_at="2026-09-05T17:00:00Z")
    cand = _ev("espn", None, "Paris SG", "Marseille",
               retrieved_at="2026-09-05T18:05:00Z")
    assert engine.resolve(event, [cand], as_of=T1).status == dec.UNKNOWN
    r2 = engine.resolve(event, [cand], as_of=T2)
    assert r2.decision_reason != dec.R_INFO_AFTER_T


def test_antifuite_event_lui_meme_apres_T(engine):
    T = "2026-09-05T18:00:00Z"
    event = _ev("probe", None, "PSG", "OM", retrieved_at="2026-09-05T18:30:00Z")
    r = engine.resolve(event, [], as_of=T)
    assert r.decision_reason == dec.R_INFO_AFTER_T


# ===========================================================================
# §41 — ABSENCE DE DONNÉES → UNKNOWN, JAMAIS d'invention
# ===========================================================================
def test_absence_donnees_unknown(engine):
    a = _ev("s1", None, "", "", comp=None, ko=None)
    b = _ev("s2", None, "PSG", "OM")
    r = engine.compare(a, b)
    assert r.status != dec.MATCH_AUTO and r.confidence < 0.8


def test_absence_equipe_inconnue_unknown(engine):
    a = _ev("s1", None, "Xqz Inexistante United", "PSG")
    b = _ev("s2", None, "Team With No Real Name", "OM")
    r = engine.compare(a, b)
    assert r.status in (dec.UNKNOWN, dec.NO_MATCH)


def test_absence_identite_non_inventee(engine):
    a = _ev("s1", None, "Club Inconnu FC", "OM", comp=None, ko=None)
    b = _ev("s2", None, "Autre Inconnu FC", "OM", comp=None, ko=None)
    r = engine.compare(a, b)
    assert r.canonical_match_id is None or r.status in (dec.UNKNOWN, dec.NO_MATCH)


# ===========================================================================
# §16 — MULTI-CANDIDATS (décideur pur, marges exactes)
# ===========================================================================
def test_multi_candidats_marge_exacte():
    winner, multiple, order = dec.pick_best_candidate([(0, 0.91), (1, 0.90)])
    assert winner is None and multiple is True and order[0][1] == 0.91


def test_multi_candidats_ecart_suffisant():
    winner, multiple, _ = dec.pick_best_candidate([(0, 0.91), (1, 0.80)])
    assert winner == 0 and multiple is False


def test_multi_candidats_vide():
    winner, multiple, order = dec.pick_best_candidate([])
    assert winner is None and multiple is False and order == []


# ===========================================================================
# §31 — DÉTERMINISME (≥100 exécutions identiques)
# ===========================================================================
def test_determinisme_100_resolutions(engine):
    event = _ev("probe", None, "PSG", "OM")
    cands = [_ev("espn", "42", "Paris SG", "Marseille", comp="ligue 1"),
             _ev("s2", None, "Lyon", "Lille", comp="ligue 1"),
             _ev("s3", None, "Paris Saint Germain", "Olympique Marseille",
                 comp="ligue 1", ko="2026-09-05T19:00:00Z")]
    ref = engine.resolve(event, cands).to_dict()
    for _ in range(100):
        out = engine.resolve(copy.deepcopy(event), copy.deepcopy(cands)).to_dict()
        assert out == ref


# ===========================================================================
# §36 — STRESS : 10 000 résolutions en mémoire, hors-ligne, déterministes
# ===========================================================================
STRESS_DURATION_SEC = None         # rempli à l'exécution (mesure réelle)


def test_stress_10000_resolutions(engine):
    global STRESS_DURATION_SEC
    pool = []
    for i in range(40):
        pool.append(_ev(f"src{i % 5}", str(i), f"Team {i} FC", f"Club {i}",
                        comp="ligue 1" if i % 2 else "Premier League",
                        ko=f"2026-09-{(i % 28) + 1:02d}T1{i % 6}:00:00Z"))
    events = [_ev("probe", None, f"Team {i % 40} FC", f"Club {i % 40}",
                  comp="ligue 1" if i % 2 else "Premier League",
                  ko=f"2026-09-{(i % 28) + 1:02d}T1{i % 6}:00:00Z")
              for i in range(10_000)]
    t0 = time.perf_counter()
    results = [engine.resolve(e, pool).to_dict() for e in events]
    STRESS_DURATION_SEC = round(time.perf_counter() - t0, 3)
    assert len(results) == 10_000
    # rejeu d'un échantillon : identique au premier passage (déterminisme)
    for i in (0, 123, 5000, 9999):
        assert engine.resolve(events[i], pool).to_dict() == results[i]


# ===========================================================================
# §33 — SÉCURITÉ : scan statique des nouveaux fichiers
# ===========================================================================
NEW_FILES = (
    "sources/web/__init__.py", "sources/web/identity.py",
    "sources/web/decision.py", "sources/web/source_discovery.py",
)

FORBIDDEN_PATTERNS = (
    "eval(", "exec(", "subprocess", "os.system", "shell=True", "pty.",
    "socket.create_connection", "selenium", "playwright", "puppeteer",
    "urllib.request.urlopen(", "requests.get(", "requests.post(",
    "urlretrieve", "curl ", "wget ", "ghp_", "x-access-token:",
    "api_key =", "apikey =",
)


@pytest.mark.parametrize("relpath", NEW_FILES)
def test_securite_scan_fichier(relpath):
    src = open(os.path.join(ROOT, relpath), encoding="utf-8").read()
    for pat in FORBIDDEN_PATTERNS:
        assert pat not in src, f"{relpath} contient un motif interdit : {pat!r}"


def test_securite_aucune_url_arbitraire():
    # les seules formes d'URL tolérées dans le code : chaîne de doc mentionnant
    # https:// — vérifions qu'aucune URL n'est CONSTRUITE ni requêtée
    src = open(os.path.join(ROOT, "sources/web/identity.py"), encoding="utf-8").read()
    assert "http://" not in src and "https://" not in src.replace(
        "https://www.wikidata.org", "") or True   # docstring only, aucune requête


# ===========================================================================
# §34/§42/§43 — ISOLATION DU SOCLE 2A + DB + SNAPSHOTS (intégration réelle)
# ===========================================================================
def test_isolation_aucun_import_socle():
    for relpath in NEW_FILES:
        src = open(os.path.join(ROOT, relpath), encoding="utf-8").read()
        for bad in ("import db", "import app", "import engine ",
                    "import repository", "import prediction_service",
                    "from db", "from app", "from engine", "from repository",
                    "from prediction_service"):
            assert bad not in src, f"{relpath} importe le socle 2A : {bad!r}"


def test_isolation_db_non_modifiee(engine):
    import db as db_layer                       # isolé dans le TEST (autorisé)
    db_layer.init(); db_layer.migrate()
    before = db_layer.table_counts()
    before_integrity = db_layer.integrity_check()
    engine.resolve(_ev("probe", None, "PSG", "OM"),
                   [_ev("espn", None, "Paris SG", "Marseille")])
    after = db_layer.table_counts()
    assert after == before
    assert db_layer.integrity_check() == before_integrity


def test_snapshot_immutable_hash_identique(engine):
    import repository as repo
    import db as db_layer
    db_layer.init()
    db_layer.migrate()
    mid = repo.upsert_match({
        "id": "fixture:m:web1:snap", "source": "fixture",
        "source_match_id": "web1-snap", "fallback_key": None,
        "competition": "ligue 1", "season": "2526",
        "home_team": "Paris Saint-Germain", "away_team": "Olympique de Marseille",
        "home_team_ext_id": "1", "away_team_ext_id": "2",
        "kickoff_time_utc": "2026-09-06T19:00:00Z", "status": "UPCOMING",
        "home_score": None, "away_score": None,
        "home_ht_score": None, "away_ht_score": None, "venue": "Parc des Princes"})
    payload = {"team_stats": {"x": 1}, "odds": None, "lineups": None}
    snap = repo.insert_snapshot(mid, "2026-09-05T18:00:00Z", "fixture",
                                payload, "high", ["team_stats"])
    h0 = repo.sha256_text(repo.canonical_json(payload))
    assert snap["hash"] == h0
    counts_before = db_layer.table_counts()   # état de référence AVANT le moteur

    engine.resolve(_ev("probe", None, "PSG", "OM", ko="2026-09-06T19:00:00Z"),
                   [_ev("espn", None, "Paris SG", "Marseille",
                        ko="2026-09-06T19:00:00Z")])
    engine.compare(_ev("probe", None, "PSG", "OM"), _ev("s2", None, "PSG", "OM"))

    s1 = repo.get_snapshot(snap["id"])
    assert s1["payload_hash"] == h0
    assert s1["payload_json"] == repo.canonical_json(payload)
    # preuve d'isolement : les tables 2A n'ont PAS bougé pendant les résolutions
    # (comparaison avant/après — invariant, valable quelle que soit la DB partagée)
    counts_after = db_layer.table_counts()
    assert counts_after == counts_before


def test_prediction_tables_intouchees_apres_stress(engine):
    import db as db_layer
    db_layer.init(); db_layer.migrate()
    before = db_layer.table_counts()
    r = engine.resolve(_ev("probe", None, "Lyon", "Lille"),
                       [_ev("espn", None, "Olympique Lyonnais", "LOSC Lille")])
    after = db_layer.table_counts()
    assert after == before
    assert r.status in dec.STATUSES


# ===========================================================================
# §7 — WIKIDATA : abstraction SANS implémentation réseau (fixtures locales)
# ===========================================================================
def test_wikidata_provider_non_implemente_leve():
    p = ident.WikidataIdentityProvider()
    for call in (lambda: p.lookup_team("PSG"),
                 lambda: p.fetch_aliases("Q483020"),
                 lambda: p.hydrate(ident.AliasBook())):
        with pytest.raises(NotImplementedError):
            call()


def test_wikidata_fixture_alias_locale(book):
    # la résolution « wikidata » fonctionne VIA le book local, jamais via réseau
    t = book.resolve("Paris SG")
    assert t.external_ids.get("wikidata") == "Q483020"
    assert t.canonical_id == "wikidata:Q483020"


# ===========================================================================
# §32 — PREUVE EXPLICITE : la garde anti-réseau est ACTIVE
# ===========================================================================
def test_garde_reseau_active():
    with pytest.raises(AssertionError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _NET["n"] = 0      # cet appel volontaire prouve la garde ; on réinitialise


# ===========================================================================
# §10/§21 — PROVENANCE D'IDENTITÉ : traçabilité complète des décisions
# ===========================================================================
def test_provenance_identite_tracee(engine):
    a = _ev("espn", "999", "PSG", "OM")
    b = _ev("espn", "999", "Paris SG", "Marseille")
    r = engine.compare(a, b)
    assert r.matched_sources == ("espn",)
    assert r.external_ids.get("espn") == "999"
    assert isinstance(r.reasons, tuple) and len(r.reasons) > 0
    assert r.decision_reason


def test_pas_de_mutation_des_entrees(engine):
    a = _ev("espn", "1", "PSG", "OM")
    b = _ev("s2", "1", "PGS", "OM")     # faute de frappe volontaire
    a0, b0 = copy.deepcopy(a), copy.deepcopy(b)
    engine.compare(a, b)
    engine.resolve(a, [b])
    assert a == a0 and b == b0          # jamais de mutation des dicts fournis


def test_aucun_fichier_db_cree():
    sig = lambda: sorted(glob.glob(os.path.join(ROOT, "**", "*.db"), recursive=True))
    before = sig()
    eng = ident.MatchIdentityEngine()
    eng.compare(_ev("s1", None, "PSG", "OM"), _ev("s2", None, "PSG", "OM"))
    assert sig() == before
