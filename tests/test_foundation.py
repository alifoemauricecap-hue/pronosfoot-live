# -*- coding: utf-8 -*-
"""
TESTS DU SOCLE DE VÉRITÉ (ÉTAPE 2A §23/§24)
===========================================
TEST 1   prédiction avant-match correctement enregistrée
TEST 2   prédiction gelée immuable (trigger SQL : ni UPDATE ni DELETE)
TEST 3   filtre temporel : aucune prédiction après le coup d'envoi (anti-fuite)
TEST 4   impossible d'insérer une prédiction sans snapshot (schema)
TEST 5   nouvelle information → nouvelle VERSION, l'ancienne reste intacte
TEST 6   redémarrage : la persistance conserve l'historique
TEST 7   deux récupérations du même match ne créent pas de doublon
TEST 8   match terminé sans prédiction pré-match → NOT_EVALUABLE
TEST 9   probabilités 1N2 = 100 % (et chaque famille somme à 100 %)
TEST 10  affichage live après redémarrage = relecture persistance (aucun recalcul)
TEST 11  SSE transporte toujours (route + trame initiale)
TEST 12  les prédictions historiques conservent leur model_version
TEST 13  règlement honnête : Brier/LogLoss bornés, gagné/perdu correct
TEST 14  anti-fuite artificielle (§24) : modifier le futur ne change RIEN au passé
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import db as db_layer
import repository as repo
import engine
import prediction_service as predsvc
import app as appmod

CODE = "test.1"


def _team(tid, sw, gf_tot, ga_tot):
    return {"id": tid, "name": f"T{tid}", "logo": None, "n": 12, "sw": sw,
            "gf": gf_tot, "ga": ga_tot,
            "home_n": 6, "home_sw": sw / 2, "home_gf": gf_tot / 2, "home_ga": ga_tot / 2,
            "away_n": 6, "away_sw": sw / 2, "away_gf": gf_tot / 2, "away_ga": ga_tot / 2,
            "w": 6, "d": 2, "l": 4, "pts": 20,
            "form": [{"r": "W", "gf": 2, "ga": 1, "day": "2026-08-30", "loc": "D", "opp": "X"}],
            "matches": []}


def make_stats():
    return {"teams": {"H": _team("H", 8.0, 14.5, 8.0), "A": _team("A", 6.0, 6.5, 9.0)},
            "league": {"n": 100, "homeAvg": 1.45, "awayAvg": 1.10}}


def make_ev(eid="E1", hours=26, state="pre", hg=None, ag=None, completed=False):
    ko = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    return {"id": eid, "utc": ko, "day": ko[:10], "state": state, "completed": completed,
            "home": {"id": "H", "name": "Home", "full": "Home FC", "logo": None, "score": hg, "ht": None},
            "away": {"id": "A", "name": "Away", "full": "Away FC", "logo": None, "score": ag, "ht": None},
            "venue": "Stade Test", "clock": "", "clockSec": 0, "detail": "FT" if state == "post" else ""}


def publish(stats=None, ev=None, detail=None):
    stats = stats or make_stats()
    ev = ev or make_ev()
    appmod.persist_match(CODE, ev)
    return predsvc.publish_match(CODE, ev, stats, detail)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    p = str(tmp_path / "test.db")
    db_layer.init(path=p, reset=True)
    repo.register_model(engine.MODEL_NAME, engine.MODEL_VERSION, engine.MODEL_CONFIG)
    with predsvc._MEM:
        predsvc._PUB.clear()
        predsvc._DISP.clear()
        predsvc._T15.clear()
        predsvc._DONE.clear()
    appmod._MATCH_MEM.clear()
    yield p


# ---------------------------------------------------------------------------
def test_01_prediction_enregistree():
    disp = publish()
    assert disp and disp["frozen"] is True and disp["frozenAt"]
    assert db_layer.table_counts()["data_snapshots"] == 1
    rows = repo.predictions_history("espn:test.1:E1")
    assert len(rows) == 6, f"6 familles attendues, reçu {len(rows)}"
    fams = {r["market"] for r in rows}
    assert fams == {"1N2", "DC", "O1.5", "O2.5", "O3.5", "BTTS"}
    for r in rows:
        assert r["prediction_status"] == "PREDICTION_FROZEN"
        assert r["frozen_at"] <= r["kickoff_time_utc"]          # gelé AVANT le match
        assert r["model_name"] == "poisson" and r["model_version"] == "1.0.0"
        assert len(r["prediction_hash"]) == 64 and r["snapshot_id"]
        assert predsvc.verify_prediction(r) is True
    # distribution 1N2 complète conservée (§13)
    r12 = [r for r in rows if r["market"] == "1N2"][0]
    dist = json.loads(r12["distribution_json"])
    assert set(dist) == {"1", "N", "2"}


def test_02_prediction_gelee_immuable():
    publish()
    row = repo.predictions_history("espn:test.1:E1")[0]
    with pytest.raises(sqlite3.IntegrityError):
        db_layer.execute("UPDATE predictions SET published_probability=0.99 WHERE id=%s", (row["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        db_layer.execute("UPDATE predictions SET model_version='9.9.9' WHERE id=%s", (row["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        db_layer.execute("DELETE FROM predictions WHERE id=%s", (row["id"],))
    # la valeur est INTACTE en base
    again = repo.get_prediction(row["id"])
    assert again["published_probability"] == row["published_probability"]


def test_03_aucune_prediction_apres_coup_d_envoi():
    ev = make_ev(hours=-1)      # coup d'envoi DÉJÀ passé
    appmod.persist_match(CODE, ev)
    disp = predsvc.publish_match(CODE, ev, make_stats(), None)
    assert disp is None, "INTERDIT de créer une prédiction après le coup d'envoi"
    assert db_layer.table_counts()["predictions"] == 0


def test_04_prediction_sans_snapshot_impossible():
    appmod.persist_match(CODE, make_ev())
    with pytest.raises(sqlite3.IntegrityError):
        db_layer.execute(
            """INSERT INTO predictions (id, match_id, version_seq, prediction_created_at,
                prediction_effective_at, kickoff_time_utc, market, selection,
                raw_probability, published_probability, model_name, model_version,
                snapshot_id, snapshot_hash, prediction_hash, prediction_status, frozen_at, created_at)
               VALUES ('x','espn:test.1:E1',1,'2026-09-04T00:00:00+00:00',
                       '2026-09-04T00:00:00+00:00',
                       '2026-09-05T00:00:00+00:00','1N2','1',0.5,0.5,'poisson','1.0.0',
                       NULL,'h','h','PREDICTION_FROZEN','2026-09-04T00:00:00+00:00',
                       '2026-09-04T00:00:00+00:00')""")


def test_05_nouvelle_info_nouvelle_version_anciennes_conservees():
    d1 = publish()                       # v1
    d2 = publish()                       # données identiques → PAS de nouvelle version
    assert d2["v"] == 1
    # nouvelle information (2 absents domicile + compositions) → NOUVELLE version (§8)
    detail = {"injuries": {"home": [{}, {}], "away": []}, "lineups": [{"x": 1}, {"y": 2}],
              "odds_probs": None, "odds_dec": None, "odds_provider": None}
    d3 = publish(detail=detail)
    assert d3["v"] == 2
    rows = repo.predictions_history("espn:test.1:E1")
    v1 = [r for r in rows if r["version_seq"] == 1]
    v2 = [r for r in rows if r["version_seq"] == 2]
    assert len(v1) == 6 and len(v2) == 6
    # v1 INTACTE après v2
    v1_after = [dict(r) for r in repo.predictions_history("espn:test.1:E1") if r["version_seq"] == 1]
    for a, b in zip(sorted(v1, key=lambda r: r["market"]), sorted(v1_after, key=lambda r: r["market"])):
        assert a["published_probability"] == b["published_probability"]
        assert a["prediction_hash"] == b["prediction_hash"]
    # journal d'audit : deux événements de gel
    evts = repo.events_for_match("espn:test.1:E1")
    assert [e for e in evts if e["event"] == "prediction_frozen"] and len(evts) >= 2


def test_06_redemarrage_conserve_historique():
    publish()
    before = db_layer.table_counts()
    rows_before = repo.predictions_history("espn:test.1:E1")
    db_layer.init(reset=True)  # ré-ouverture du MÊME fichier = redémarrage serveur
    after = db_layer.table_counts()
    assert before == after
    rows_after = repo.predictions_history("espn:test.1:E1")
    assert [r["prediction_hash"] for r in rows_before] == [r["prediction_hash"] for r in rows_after]


def test_07_pas_de_doublon_match_ni_prediction():
    disp1 = publish()
    disp2 = publish()
    assert disp1["v"] == disp2["v"] == 1
    ev = make_ev()
    appmod.persist_match(CODE, ev)   # ré-ingestion du MÊME match
    appmod.persist_match(CODE, ev)
    assert db_layer.table_counts()["matches"] == 1
    assert db_layer.table_counts()["predictions"] == 6


def test_08_match_termine_sans_prono_not_evaluable():
    ev = make_ev(hours=-3, state="post", hg="2", ag="1", completed=True)
    appmod.persist_match(CODE, ev)
    out = predsvc.settle_if_finished(CODE, ev)
    assert out and out["status"] == "NOT_EVALUABLE"
    m = repo.get_match("espn:test.1:E1")
    assert m["evaluation_status"] == "NOT_EVALUABLE"
    assert db_layer.table_counts()["prediction_results"] == 0
    # et le résultat réel, lui, est bien archivé (donnée réelle, pas prédiction fabriquée)
    res = repo.get_result("espn:test.1:E1")
    assert res["home_score"] == 2 and res["away_score"] == 1


def test_09_probabilites_totalisent_100():
    publish()
    rows = {r["market"]: r for r in repo.predictions_history("espn:test.1:E1")}
    for market, r in rows.items():
        dist = json.loads(r["distribution_json"])
        if market == "DC":
            # legs NON exclusives : cohérence avec 1N2 au lieu de la somme 100 %
            d12 = json.loads(rows["1N2"]["distribution_json"])
            assert abs(dist["1N"] - (d12["1"] + d12["N"])) < 0.02
            assert abs(dist["N2"] - (d12["N"] + d12["2"])) < 0.02
            assert abs(dist["12"] - (d12["1"] + d12["2"])) < 0.02
        else:
            tot = sum(dist.values())
            assert abs(tot - 1.0) < 0.01, f"{market} totalise {tot}"
    # §13 : la distribution 1X2 publiée somme à 100 % (tolérance numérique)
    d12 = json.loads(rows["1N2"]["distribution_json"])
    assert abs(d12["1"] + d12["N"] + d12["2"] - 1.0) < 0.005


def test_10_live_apres_redemarrage_relit_la_persistance():
    d = publish()
    predsvc.invalidate_all()             # RAM vidée (simule un redémarrage)
    with predsvc._MEM:
        predsvc._DONE.clear(); predsvc._T15.clear()
    ev_live = make_ev(state="in", hg="0", ag="0")
    appmod.persist_match(CODE, ev_live)
    disp = predsvc.frozen_display(CODE, ev_live)
    assert disp is not None and disp["frozen"] is True
    assert disp["p1"] == d["p1"] and disp["v"] == 1   # IDENTIQUE : aucun recalcul live


def test_11_sse_toujours_en_marche():
    resp = appmod.api_stream()
    assert resp.mimetype == "text/event-stream"
    gen = iter(resp.response)
    first = next(gen)
    assert "retry" in first                      # trame SSE initiale présente
    gen.close()
    with appmod.app.test_request_context():
        rules = {r.rule for r in appmod.app.url_map.iter_rules()}
    assert "/api/stream" in rules and "/api/feed" in rules


def test_12_model_version_conservee_dans_historique():
    publish()
    repo.register_model("poisson", "1.1.0", {"nouveau": True})   # futur modèle
    rows = repo.predictions_history("espn:test.1:E1")
    assert all(r["model_version"] == "1.0.0" for r in rows), \
        "une prédiction historique doit rester liée à son modèle d'origine"


def test_13_reglement_honnete_brier_logloss():
    publish()
    ev_post = make_ev(state="post", hg="2", ag="1", completed=True)
    appmod.persist_match(CODE, ev_post)
    out = predsvc.settle_if_finished(CODE, ev_post)
    assert out["status"] == "SETTLED" and out["markets_scored"] == 6
    rows = repo.predictions_history("espn:test.1:E1")
    for r in rows:
        assert r["prediction_status"] == "SETTLED"
        pr = db_layer.query("SELECT * FROM prediction_results WHERE prediction_id=%s", (r["id"],), one=True)
        assert pr is not None, f"pas de règlement pour {r['market']}"
        assert 0.0 <= pr["brier_score"] <= 2.0, "Brier multi-classes borné [0,2]"
        assert pr["log_loss"] > 0
        assert pr["won"] in (0, 1)
    r12 = [r for r in rows if r["market"] == "1N2"][0]
    pr12 = db_layer.query("SELECT * FROM prediction_results WHERE prediction_id=%s", (r12["id"],), one=True)
    assert pr12["actual_outcome"] == "1"              # 2-1 → victoire domicile
    assert pr12["won"] == (1 if r12["selection"] == "1" else 0)
    assert all(predsvc.verify_prediction(r) for r in rows)
    # métriques publiques : échantillon < 20 → « rebuilding », RIEN d'inventé
    perf = predsvc.public_performance(force=True)
    assert perf["status"] == "rebuilding" and perf["settledMatches"] == 1


def test_15_pas_de_version_pingpong():
    """Une donnée oscillante (A→B→A) ne crée PAS v3 identique à v1 : on réutilise
    la version gelée existante correspondant exactement au même jeu de données."""
    publish()                                   # v1 (données A)
    detail = {"injuries": {"home": [{}], "away": []}, "lineups": None,
              "odds_probs": None, "odds_dec": None, "odds_provider": None}
    d2 = publish(detail=detail)                 # v2 (données B)
    assert d2["v"] == 2
    predsvc.invalidate_all()                    # RAM vidée : retour aux données A
    d3 = publish(detail=None)                   # A connu → PAS de v3 ping-pong
    assert d3["v"] == 1, f"v3 indésirable (ping-pong), reçu v{d3['v']}"
    assert db_layer.table_counts()["predictions"] == 12   # 2 versions × 6 familles, pas plus


def test_14_anti_fuite_modification_du_futur():
    """§24 — test anti-fuite automatique : modifier artificiellement le futur
    ne doit changer NI les features NI aucune valeur de la prédiction gelée."""
    publish()
    before = [dict(r) for r in repo.predictions_history("espn:test.1:E1")]
    snap_before = repo.get_snapshot(before[0]["snapshot_id"])["payload_json"]
    # — modification artificielle du résultat futur (scandaleux 9-0) —
    repo.insert_result("espn:test.1:E1", 9, 0, 5, 0, "FT", db_layer.utcnow(), "espn")
    # — relecture : les features du snapshot et les prédictions gelées n'ont pas bougé
    after = [dict(r) for r in repo.predictions_history("espn:test.1:E1")]
    snap_after = repo.get_snapshot(after[0]["snapshot_id"])["payload_json"]
    assert snap_before == snap_after, "LES FEATURES ONT CHANGÉ APRÈS LE MATCH — FUITE !"
    for a, b in zip(before, after):
        for k in ("market", "selection", "raw_probability", "published_probability",
                  "model_version", "snapshot_hash", "prediction_hash", "frozen_at", "kickoff_time_utc"):
            assert a[k] == b[k], f"LA PRÉDICTION GELÉE A CHANGÉ ({k}) — FUITE !"
        assert predsvc.verify_prediction(b) is True
    # — et l'affichage live/terminé continue de relire la prédiction gelée identique
    predsvc.invalidate_all()
    disp = predsvc.frozen_display(CODE, make_ev(state="post", hg="9", ag="0", completed=True), with_verdicts=True)
    assert disp is not None
