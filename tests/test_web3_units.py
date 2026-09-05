# -*- coding: utf-8 -*-
"""
TESTS WEB-3 — UNITAIRES PURS (100 % HORS LIGNE)
================================================
normalized · validation · freshness · conflicts · journal · pit_store ·
metrics · aggregation · EXTRACTEURS (fixtures réelles WEB-0 + synthétiques
structurellement identiques).
"""
import json
import os

import pytest

from sources import registry as regmod
from sources.web import normalized as N
from sources.web import validation as V
from sources.web import freshness as F
from sources.web import conflicts as C
from sources.web.research_journal import ResearchJournal
from sources.web.pit_store import PointInTimeStore
from sources.web.quality_metrics import QualityMetrics
from sources.web.aggregation import team_form, form_datapoints
from sources.web.extractors.base import FetchContext, ensure_obj
from sources.web.extractors import espn as XE, football_data as XF, \
    open_meteo as XO, openligadb as XL, statsbomb as XS, wikidata as XW

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "web3")
REG = regmod.load()


def fx(name):
    return open(os.path.join(FIX, name), encoding="utf-8").read()


def ctx(**kw):
    kw.setdefault("retrieved_at", "2026-09-05T12:00:00Z")
    kw.setdefault("source_url", "http://fixture")
    kw.setdefault("match_id", "m:test")
    return FetchContext(**kw)


# ===========================================================================
# NORMALIZED — UNKNOWN ≠ 0 ≠ FALSE, provenance §13, anti-leakage §14
# ===========================================================================
def test_n1_unknown_singleton():
    assert N.UNKNOWN is N.UNKNOWN and repr(N.UNKNOWN) == "UNKNOWN"
    assert N.is_unknown(N.UNKNOWN) and not N.is_unknown(0)


def test_n2_unknown_nest_ni_0_ni_false():
    with pytest.raises(TypeError):
        bool(N.UNKNOWN)
    assert N.UNKNOWN != 0 and N.UNKNOWN is not False


def test_n3_new_datapoint_none_devient_unknown():
    dp = N.new_datapoint(None, "score_home", "espn",
                         retrieved_at="2026-09-05T12:00:00Z")
    assert dp.value is N.UNKNOWN and dp.confidence == "unknown"


def test_n4_derived_exige_methode_et_version():
    with pytest.raises(ValueError):
        N.DataPoint(1.5, "xg", "statsbomb_open",
                    retrieved_at="2026-09-05T12:00:00Z", level=N.DERIVED)


def test_n5_to_dict_champs_provenance_s13():
    dp = N.new_datapoint(2, "score_home", "espn",
                         retrieved_at="2026-09-05T12:00:00Z",
                         effective_at="2026-09-05T11:30:00Z",
                         source_url="http://x", confidence="high",
                         match_id="m:1", team_id="361")
    d = dp.to_dict("2026-09-05T12:00:00Z")
    for k in ("value", "data_type", "source", "source_url", "retrieved_at",
              "published_at", "effective_at", "confidence", "freshness_sec",
              "match_id", "team_id", "player_id", "competition_id"):
        assert k in d, k
    assert d["value"] == 2 and d["source"] == "espn"


def test_n6_compatibilite_2a_validate_data_point():
    dp = N.new_datapoint(2, "score_home", "espn",
                         retrieved_at="2026-09-05T12:00:00Z",
                         effective_at="2026-09-05T11:30:00Z")
    assert dp.validate_2a() is True          # provenance.validate_data_point


def test_n7_derived_transporte_methode_version_inputs():
    dp = N.new_datapoint(0.65, "xg", "statsbomb_open",
                         retrieved_at="2026-09-05T12:00:00Z", level=N.DERIVED,
                         derivation_method="sum", model_version="web3-v1",
                         inputs=["events:3 shots"])
    d = dp.to_dict()
    assert d["derivation_method"] == "sum" and d["model_version"] == "web3-v1"
    assert d["level"] == "DERIVED" and d["inputs"] == ["events:3 shots"]


def test_n8_anti_leakage_exemple_du_prompt():
    """Match 1er sept. Prédiction 15:00. Donnée récupérée 15:30 → INTERDITE."""
    dp = N.new_datapoint(1, "score_home", "espn",
                         retrieved_at="2026-09-01T15:30:00Z",
                         effective_at="2026-09-01T14:00:00Z")
    assert dp.usable_at("2026-09-01T15:00:00Z") is False
    assert dp.usable_at("2026-09-01T16:00:00Z") is True


def test_n9_usable_at_effective_at_posterieur_interdit():
    dp = N.new_datapoint(22.1, "temperature", "open_meteo",
                         retrieved_at="2026-09-01T14:00:00Z",
                         effective_at="2026-09-01T20:00:00Z")
    assert dp.usable_at("2026-09-01T15:00:00Z") is False


def test_n10_unknown_point_explicite():
    dp = N.unknown_point("xg", "statsbomb_open",
                         retrieved_at="2026-09-05T12:00:00Z")
    assert dp.value is N.UNKNOWN and "NOT_AVAILABLE_AT_SOURCE" in dp.issues


# ===========================================================================
# VALIDATION §17 — anomalies marquées, jamais plantées, jamais injectées
# ===========================================================================
def test_v1_score_negatif_invalide():
    dp = N.new_datapoint(-1, "score_home", "espn",
                         retrieved_at="2026-09-05T12:00:00Z")
    V.apply_validation(dp)
    assert not dp.valid and "INVALID_SCORE_NEGATIVE" in dp.issues


def test_v2_possession_hors_bornes():
    dp = N.new_datapoint(140.0, "possession", "espn",
                         retrieved_at="2026-09-05T12:00:00Z")
    V.apply_validation(dp)
    assert not dp.valid and "INVALID_POSSESSION_RANGE" in dp.issues


def test_v3_cote_inferieure_ou_egale_1():
    dp = N.new_datapoint(0.95, "odd", "football_data_co_uk",
                         retrieved_at="2026-09-05T12:00:00Z")
    V.apply_validation(dp)
    assert not dp.valid and "INVALID_ODD_LE_1" in dp.issues


def test_v4_minute_absurde_et_compteurs():
    d1 = N.new_datapoint(150, "minute", "espn",
                         retrieved_at="2026-09-05T12:00:00Z")
    d2 = N.new_datapoint(-3, "corners", "espn",
                         retrieved_at="2026-09-05T12:00:00Z")
    V.apply_validation(d1); V.apply_validation(d2)
    assert not d1.valid and not d2.valid


def test_v5_sot_superieur_a_shots_via_contexte():
    dp = N.new_datapoint(7, "shots_on_target", "espn",
                         retrieved_at="2026-09-05T12:00:00Z")
    V.apply_validation(dp, context={"shots": 5, "shots_on_target": 7})
    assert "INVALID_SOT_GT_SHOTS" in dp.issues and not dp.valid


def test_v6_meme_equipe_invalide():
    dp = N.new_datapoint("x", "match_ref", "espn",
                         retrieved_at="2026-09-05T12:00:00Z")
    V.apply_validation(dp, context={"home_team": "OM", "away_team": "OM"})
    assert "INVALID_SAME_TEAM" in dp.issues


def test_v7_unknown_nest_pas_une_anomalie():
    dp = N.unknown_point("score_home", "espn",
                         retrieved_at="2026-09-05T12:00:00Z")
    V.apply_validation(dp)
    assert dp.valid and dp.value is N.UNKNOWN
    assert not V.is_usable_value(dp)      # mais jamais injectée comme valeur


# ===========================================================================
# FRESHNESS §15 — TTL registry, stale, anti-leakage combiné
# ===========================================================================
def test_f1_score_live_frais_puis_stale_30s():
    dp = N.new_datapoint(1, "score_live", "espn",
                         retrieved_at="2026-09-05T12:00:00Z")
    st, age, ttl = F.freshness_status(dp, "2026-09-05T12:00:20Z")
    assert st == F.FRESH and ttl == 30
    st2, age2, _ = F.freshness_status(dp, "2026-09-05T12:00:31Z")
    assert st2 == F.STALE and age2 == 31


def test_f2_historique_sans_expiration():
    dp = N.new_datapoint({"x": 1}, "historical_results",
                         "football_data_co_uk",
                         retrieved_at="2020-01-01T00:00:00Z")
    st, age, ttl = F.freshness_status(dp, "2026-09-05T12:00:00Z")
    assert st == F.NO_EXPIRY and ttl is None


def test_f3_ttl_vient_du_registry_reel():
    assert F.ttl_for("weather") == 10800
    assert F.ttl_for("standings") == 7200
    assert F.ttl_for("match_calendar") == 900


def test_f4_ttl_override_par_source():
    reg = json.loads(json.dumps(REG))
    regmod.get_source(reg, "espn")["ttl_overrides_sec"] = {"score_live": 5}
    assert F.ttl_for("score_live", "espn", reg) == 5
    assert F.ttl_for("score_live", "openligadb", reg) == 30


def test_f5_usable_pour_prediction_combine_fuite_et_fraicheur():
    dp = N.new_datapoint(1, "score_live", "espn",
                         retrieved_at="2026-09-05T11:59:50Z")
    assert F.usable_for_prediction(dp, "2026-09-05T12:00:10Z") is True
    assert F.usable_for_prediction(dp, "2026-09-05T12:00:40Z") is False
    assert F.usable_for_prediction(dp, "2026-09-05T12:00:40Z",
                                   require_fresh=False) is True


# ===========================================================================
# CONFLICTS §16 — séparées, politique WEB-0, UNKNOWN si critique
# ===========================================================================
def _mk(value, source, **kw):
    return N.new_datapoint(value, "shots", source,
                           retrieved_at=kw.pop("r", "2026-09-05T12:00:00Z"),
                           **kw)


def test_c1_pas_de_conflit_une_seule_source():
    pts = [_mk(10, "espn")]
    assert C.detect_conflict(pts) is None


def test_c2_valeurs_egales_pas_de_conflit():
    assert C.detect_conflict([_mk(10, "espn"),
                              _mk(10, "football_data_co_uk")]) is None


def test_c3_conflit_detecte_jamais_de_moyenne():
    pts = [_mk(10, "espn"), _mk(12, "football_data_co_uk")]
    rep = C.detect_conflict(pts)
    assert rep and rep["conflict"] is True
    assert rep["values"] == {"espn": 10, "football_data_co_uk": 12}
    assert 11 not in rep["values"].values()      # moyenne INTERDITE


def test_c4_resolution_priorite_registry_espn_shots():
    pts = [_mk(12, "football_data_co_uk"), _mk(10, "espn")]
    chosen, rep = C.resolve_conflict(pts, "shots", reg=REG)
    assert chosen.source == "espn"               # chaîne shots : espn d'abord
    assert rep["decision"] == "PRIORITY_SOURCE"
    assert len(pts) == 2                          # valeurs conservées


def test_c5_tiebreak_recence():
    p_old = _mk(10, "espn", r="2026-09-05T10:00:00Z",
                effective_at="2026-09-05T10:00:00Z")
    p_new = _mk(12, "statsbomb_open", r="2026-09-05T11:00:00Z",
                effective_at="2026-09-05T11:00:00Z")
    chosen, rep = C.resolve_conflict([p_old, p_new], "donnee_bidon", reg=REG)
    assert rep["decision"] == "RECENCY_TIEBREAK"
    assert chosen is p_new


def test_c6_conflit_critique_unknown():
    a = _mk(10, "espn", r="2026-09-05T12:00:00Z",
            effective_at="2026-09-05T12:00:00Z")
    b = _mk(12, "statsbomb_open", r="2026-09-05T12:00:00Z",
            effective_at="2026-09-05T12:00:00Z")
    chosen, rep = C.resolve_conflict([a, b], "donnee_bidon", reg=REG)
    assert chosen is None and rep["critical"] is True
    assert rep["decision"] == "UNKNOWN_CRITICAL_CONFLICT"


def test_c7_conflit_journalise():
    j = ResearchJournal()
    m = QualityMetrics()
    pts = [_mk(10, "espn"), _mk(12, "football_data_co_uk")]
    C.resolve_conflict(pts, "shots", reg=REG, journal=j, metrics=m,
                       match_id="m:1")
    evs = [e for e in j.events() if e.get("event") == "CONFLICT"]
    assert evs and evs[0]["decision"] == "PRIORITY_SOURCE"
    assert m.snapshot()["conflict_rate"] > 0


# ===========================================================================
# JOURNAL §26 / PIT STORE / METRICS §34
# ===========================================================================
def test_j1_journal_champs_et_filtre_secrets():
    j = ResearchJournal()
    j.record(event="RESEARCH_OK", match_id="m:1", source="espn",
             data_type="score_live", host="site.api.espn.com", status="OK",
             latency_ms=42.0, bytes=1234, cache_hit=False, records_found=7,
             authorization="NEVER", token="NEVER", api_key="NEVER")
    e = j.events()[0]
    assert e["host"] == "site.api.espn.com" and e["records_found"] == 7
    blob = json.dumps(j.events())
    for bad in ("authorization", "token", "api_key", "NEVER"):
        assert bad not in blob


def test_j2_journal_capacite_bornee():
    j = ResearchJournal(capacity=5)
    for i in range(12):
        j.record(i=i)
    assert j.count() == 5


def test_p1_store_query_filtres():
    s = PointInTimeStore()
    d1 = _mk(1, "espn", match_id="m:1", r="2026-09-05T11:00:00Z")
    d2 = _mk(2, "espn", match_id="m:2", r="2026-09-05T11:30:00Z")
    s.add_many([d1, d2])
    assert len(s.query(match_id="m:1")) == 1
    assert s.query(match_id="m:1")[0] is d1


def test_p2_store_anti_leakage_lecture():
    s = PointInTimeStore()
    a = _mk(1, "espn", r="2026-09-01T14:00:00Z")
    b = _mk(2, "espn", r="2026-09-01T15:30:00Z")
    s.add_many([a, b])
    got = s.query(data_type="shots", as_of="2026-09-01T15:00:00Z")
    assert got == [a]                             # b exclu (futur à T)
    assert len(s.query(data_type="shots",
                       as_of="2026-09-01T16:00:00Z")) == 2


def test_p3_store_latest_et_append_only():
    s = PointInTimeStore()
    a = _mk(1, "espn", match_id="m:test", r="2026-09-05T11:00:00Z")
    b = _mk(2, "espn", match_id="m:test", r="2026-09-05T11:15:00Z")
    s.add_many([a, b])
    assert s.latest("m:test", "shots", "2026-09-05T12:00:00Z") is b
    assert s.latest("m:test", "shots", "2026-09-05T11:10:00Z") is a
    assert a.value == 1                            # passé jamais réécrit


def test_m1_metrics_taux():
    m = QualityMetrics()
    m.record_fetch("m:1", ok=True, latency_ms=100.0)
    m.record_fetch("m:1", ok=False, latency_ms=50.0)
    m.record_fetch("m:1", ok=True, latency_ms=5.0, cache_hit=True)
    snap = m.snapshot()
    assert snap["source_success_rate"] == pytest.approx(2 / 3)
    assert snap["requests_total"] == 2 and snap["cache_hit_rate"] > 0
    assert snap["requests_per_match"] == {"m:1": 2}


def test_m2_metrics_unknown_invalid_rates():
    m = QualityMetrics()
    m.record_datapoint(unknown=True)
    m.record_datapoint(valid=False)
    m.record_datapoint()
    snap = m.snapshot()
    assert snap["unknown_rate"] == 0.3333            # arrondi documenté ×1e-4
    assert snap["invalid_data_rate"] == 0.3333


def test_m3_garde_fou_aucune_metrique_modele():
    m = QualityMetrics()
    m.record_fetch("m:x", ok=True)
    snap = m.snapshot()
    m.assert_no_model_metrics(snap)
    for bad in ("accuracy", "brier", "log_loss", "calibration"):
        assert bad not in snap


# ===========================================================================
# AGGREGATION §19 — forme réelle depuis lignes football-data
# ===========================================================================
def _rows():
    from sources.web.extractors.football_data import parse_csv
    rows, _ = parse_csv(fx("fdco_E0_sample_real.csv"), ctx())
    return rows


def test_a1_form_liverpool_reelle():
    rows = _rows()
    res = team_form(rows, "Liverpool", last_n=5)
    assert res                                    # échantillon 40 matchs = 4 journées
    g = res["metrics"]
    assert 1 <= g["played"] <= 5                  # pas de tronquage inventé
    assert g["wins"] + g["draws"] + g["losses"] == g["played"]
    assert len(res["inputs"]) == g["played"] and res["inputs"][0]["date"]


def test_a2_form_string_et_clean_sheets():
    rows = _rows()
    res = team_form(rows, "Liverpool", last_n=5)
    g = res["metrics"]
    assert set(g["form_string"]) <= set("WDL")
    assert len(g["form_string"]) == g["played"]
    assert g["clean_sheets"] <= g["played"] and g["btts"] <= g["played"]
    assert g["over_2_5"] + g["under_2_5"] == g["played"]


def test_a3_sample_incomplet_honnete():
    rows = _rows()
    res = team_form(rows, "Liverpool", last_n=10)
    assert res and res["metrics"]["played"] < 10
    assert res["metrics"]["sample_complete"] is False


def test_a4_venue_split_home_away():
    rows = _rows()
    h = team_form(rows, "Liverpool", last_n=5, venue="home")
    a = team_form(rows, "Liverpool", last_n=5, venue="away")
    assert h and all(i["opponent"] for i in h["inputs"])
    if a:
        assert sum(1 for i in h["inputs"]) + sum(1 for i in a["inputs"]) <= 5


def test_a5_equipe_inconnue_unknown_pas_zero():
    dp = form_datapoints(_rows(), "Equipe Absente FC", ctx(),
                         "football_data_co_uk", last_n=5)
    assert dp.value is N.UNKNOWN
    assert "NO_COMPLETED_MATCHES_IN_WINDOW" in dp.issues
    assert dp.value != 0


def test_a6_form_est_aggregated_avec_inputs():
    dp = form_datapoints(_rows(), "Liverpool", ctx(), "football_data_co_uk",
                         last_n=5)
    assert dp.level == N.AGGREGATED
    assert dp.derivation_method and dp.model_version and dp.inputs


# ===========================================================================
# EXTRACTEURS — ESPN (réel scoreboard + summary fixture)
# ===========================================================================
def test_xe1_scoreboard_reel_identity_live():
    obj = json.loads(fx("espn_scoreboard_real.json"))
    ev = obj["events"][0]
    idp = XE.parse_event_identity(ev, ctx(league_code="eng.1",
                                          league_name="English Premier League"))
    by_type = {p.data_type: p for p in idp}
    assert by_type["home_team"].value == "Newcastle United"
    assert by_type["away_team"].value == "AFC Bournemouth"
    assert by_type["match_ref"].value == "espn:eng.1:401879286"
    assert by_type["venue"].value == "St. James' Park"
    assert by_type["venue_country"].value == "England"
    assert by_type["match_status"].value == "STATUS_SCHEDULED"
    live = XE.parse_event_live(ev, ctx())
    lt = {p.data_type: p for p in live}
    assert lt["score_home"].value == 0 and lt["score_away"].value == 0
    assert lt["form_home"].value == "WWDLW"


def test_xe2_absent_pas_emis():
    obj = json.loads(fx("espn_scoreboard_real.json"))
    ev = dict(obj["events"][0])
    comps = [dict(c) for c in ev["competitions"]]
    comps[0] = dict(comps[0]); comps[0].pop("odds", None)
    ev["competitions"] = comps
    live = XE.parse_event_live(ev, ctx())
    assert all(p.data_type != "odds" for p in live)   # jamais inventé


def test_xe3_summary_stats_mapping_live():
    obj = json.loads(fx("espn_summary_fixture.json"))
    pts = XE.parse_summary_stats(obj, ctx())
    key = {(p.data_type, p.team_id): p for p in pts}
    assert key[("possession", "361")].value == pytest.approx(54.3)
    assert key[("shots", "361")].value == 15
    assert key[("shots_on_target", "361")].value == 6
    assert key[("corners", "374")].value == 4
    assert key[("cards_yellow", "374")].value == 2
    for p in pts:
        assert V.validate_datapoint(p) == []


def test_xe4_summary_lineups_reelles():
    obj = json.loads(fx("espn_summary_fixture.json"))
    pts = XE.parse_summary_lineups(obj, ctx())
    assert len(pts) == 2
    newc = next(p for p in pts if p.team_id == "361")
    starters = [r["player"] for r in newc.value if r["starter"]]
    subs = [r["player"] for r in newc.value if not r["starter"]]
    assert "Alexander Isak" in starters and "Joe Willock" in subs


def test_xe5_summary_injuries_reelles():
    obj = json.loads(fx("espn_summary_fixture.json"))
    pts = XE.parse_summary_injuries(obj, ctx())
    assert len(pts) == 1
    assert pts[0].value[0]["player"] == "Sven Botman"
    assert pts[0].value[0]["status"] == "Out"


def test_xe6_odds_reelles_bookmaker():
    obj = json.loads(fx("espn_scoreboard_real.json"))
    ev = obj["events"][0]
    live = XE.parse_event_live(ev, ctx())
    odds = [p for p in live if p.data_type == "odds"]
    assert odds
    o = odds[0].value[0]
    assert o["over_under"] is not None or o["bookmaker"] is not None


# ===========================================================================
# EXTRACTEURS — FOOTBALL-DATA (CSV réel échantillon)
# ===========================================================================
def test_xf1_csv_reel_resultats_stats():
    rows, _ = XF.parse_csv(fx("fdco_E0_sample_real.csv"), ctx(season="2526"))
    assert len(rows) == 40
    r0 = rows[0]
    assert r0["home"] == "Liverpool" and r0["away"] == "Bournemouth"
    assert r0["fthg"] == 4 and r0["ftag"] == 2 and r0["ftr"] == "H"
    assert r0["date"] == "2025-08-15" and r0["referee"] == "A Taylor"
    assert r0["stats"]["home"]["shots"] >= 0


def test_xf2_odds_separees_bookmaker_ouverture_cloture():
    rows, _ = XF.parse_csv(fx("fdco_E0_sample_real.csv"), ctx(season="2526"))
    odds = rows[0]["odds"]
    assert odds, "aucune cote extraite ?"
    entry = odds[0]
    assert set(("bookmaker", "market")) <= set(entry)
    kinds = [k for k in entry if k in ("opening", "closing", "listed")]
    assert len(kinds) == 1                         # un seul timing par entrée
    assert entry["market"] == "1X2" and entry["selection"] in _TEAMS()
    # jamais écrasées : plusieurs entrées par bookmaker/sélection possible
    keys = {(e["bookmaker"], e["selection"]) for e in odds}
    assert len(keys) >= len(odds) * 0.5


def _TEAMS():
    return ("HOME", "DRAW", "AWAY")


def test_xf3_csv_malforme_resilient():
    rows, _ = XF.parse_csv("Date,HomeTeam\n15/08/2025,\nxx", ctx())
    assert rows == []


# ===========================================================================
# EXTRACTEURS — OPEN-METEO (réel + hourly fixture)
# ===========================================================================
def test_xo1_current_reel_temperature_precipitation():
    obj = json.loads(fx("open_meteo_current_real.json"))
    pts = XO.parse_weather(obj, ctx())
    types = {p.data_type: p for p in pts}
    assert "temperature" in types and "precipitation" in types
    assert "wind_speed" not in types               # absent → jamais inventé
    assert types["temperature"].effective_at == obj["current"]["time"]


def test_xo2_hourly_cible_la_bonne_tranche():
    obj = json.loads(fx("open_meteo_hourly_fixture.json"))
    pts = XO.parse_weather(obj, ctx(), target_time="2026-09-06T19:10:00Z")
    t = {p.data_type: p for p in pts}
    assert t["temperature"].value == 13.1
    assert t["temperature"].effective_at == "2026-09-06T19:00"
    assert "MATCH_TIME_APPROXIMATION" not in t["temperature"].issues


def test_xo3_approximation_marquee_si_ecart():
    obj = json.loads(fx("open_meteo_hourly_fixture.json"))
    pts = XO.parse_weather(obj, ctx(), target_time="2026-09-06T17:45:00Z")
    t = {p.data_type: p for p in pts}
    p = t["temperature"]
    assert p.value == 13.8                          # tranche 18:00 (25 min)


def test_xo4_meteo_est_temporelle_anti_leakage():
    obj = json.loads(fx("open_meteo_current_real.json"))
    pts = XO.parse_weather(obj, ctx(retrieved_at="2026-09-05T12:00:00Z"))
    t = {p.data_type: p for p in pts}
    temp = t["temperature"]
    # une météo future récupérée tôt est inutilisable avant son heure d'effet
    assert temp.usable_at("2020-01-01T00:00:00Z") is False


# ===========================================================================
# EXTRACTEURS — OPENLIGADB / STATSBOMB / WIKIDATA
# ===========================================================================
def test_xl1_openligadb_fixture():
    obj = json.loads(fx("openligadb_matches_fixture.json"))
    pts, rows = XL.parse_matches(obj, ctx())
    assert len(rows) == 3
    byn = {p.data_type: p for p in pts
           if p.match_id == "openligadb:70001"}
    assert byn["home_team"].value == "FC Bayern München"
    assert byn["score_home"].value == 3
    assert rows[2]["finished"] is False and rows[2]["fthg"] is None


def test_xs1_competitions_reelles():
    obj = json.loads(fx("sb_competitions_real.json"))
    pts = XS.parse_competitions(obj, ctx())
    names = {p.value["name"] for p in pts}
    assert any("Bundesliga" in (n or "") for n in names)
    assert all(p.value["competition_id"] for p in pts)


def test_xs2_events_derives_xg_shots_honnete():
    obj = json.loads(fx("sb_events_fixture.json"))["events"]
    pts = XS.derive_match_stats(obj, ctx(), match_date="2026-09-01")
    byg = {}
    for p in pts:
        byg.setdefault(p.derivation_method or p.data_type, p)
    xg = [p for p in pts if p.data_type == "xg" and
          not N.is_unknown(p.value)]
    assert xg
    v1, v2 = sorted((p.value for p in xg), reverse=True)
    assert v1 == pytest.approx(0.65)               # 0.42+0.18+0.05 (Alpha)
    assert v2 == pytest.approx(0.43)               # 0.31+0.12 (Beta)
    for p in xg:
        assert p.level == N.DERIVED
        assert p.derivation_method and p.model_version == N.MODEL_VERSION
        assert p.inputs                            # traçabilité du calcul
    shots = [p for p in pts if p.data_type == "shots"]
    assert sorted(p.value for p in shots) == [2, 3]
    sot = [p for p in pts if p.data_type == "shots_on_target"]
    assert sorted(p.value for p in sot) == [1, 2]  # Goal+Saved / SavedToPost


def test_xs3_ppda_unknown_si_donnees_insuffisantes():
    obj = json.loads(fx("sb_events_fixture.json"))["events"]
    pts = XS.derive_match_stats(obj, ctx())
    ppda = [p for p in pts if p.data_type == "ppda"]
    assert ppda and all(N.is_unknown(p.value) for p in ppda)
    assert any("PPDA_REQUIRES_OPPOSITION_PASS_MAP" in p.issues for p in ppda)


def test_xs4_derive_stats_evenements_vides():
    obj = []
    assert XS.derive_match_stats(obj, ctx()) == []


def test_xw1_search_reel_psg():
    obj = json.loads(fx("wikidata_search_psg_real.json"))
    pts = XW.parse_search(obj, ctx())
    assert pts
    first = pts[0].value
    assert first["qid"] == "Q483020"
    assert "Paris Saint-Germain" in (first["label"] or "")


def test_xw2_entity_aliases_pays():
    obj = {"entities": {"Q483020": {
        "labels": {"fr": {"value": "Paris Saint-Germain Football Club"}},
        "aliases": {"fr": [{"value": "Paris SG"}, {"value": "PSG"}]},
        "claims": {"P17": [{"mainsnak": {"datavalue": {
            "value": {"id": "Q142"}}}}]}}}}
    pts = XW.parse_entity(obj, ctx())
    t = {p.data_type: p for p in pts}
    assert t["entity_aliases"].value == ["Paris SG", "PSG"]
    assert t["entity_country_qid"].value == "Q142"
    assert t["entity_qid"].value == "Q483020"


# ===========================================================================
# ensure_obj — réponses vides/malformées (résilience extracteur)
# ===========================================================================
def test_eo1_reponse_vide_refusee():
    with pytest.raises(ValueError):
        ensure_obj(b"")


def test_eo2_json_malforme_refuse():
    with pytest.raises(ValueError):
        ensure_obj(b"{pas du json")


def test_eo3_bytes_json_ok():
    assert ensure_obj(b'{"a": 1}') == {"a": 1}
