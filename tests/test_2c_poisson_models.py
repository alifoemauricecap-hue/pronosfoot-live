# -*- coding: utf-8 -*-
"""2C §8/§15/§21/§23 — mathématiques, modèles extrêmes, NO_PREDICTION."""
import math
import pytest
from model2c import poisson2c as pz
from model2c.poisson2c import poisson_pmf, score_matrix, markets_from_matrix, dc_tau
from model2c.models import Model2C_A, Model2C_B, Model2C_C, Model2C_D, predict_all
from model2c.guards import (data_level, prediction_allowed, publish_guard,
                            clamp_prob, FULL_DATA, LOW_DATA, INSUFFICIENT_DATA,
                            NO_PREDICTION, metric_verdict)
from model2c.dc import DixonColesParams


class TestPoissonMath:
    def test_pmf_sums_near_one(self):
        assert abs(sum(poisson_pmf(1.7, 12)) - 1.0) < 1e-6

    def test_pmf_zero_lambda_rejected(self):
        with pytest.raises(ValueError):
            poisson_pmf(0.0, 5)

    def test_matrix_normalized(self):
        m, lh, la = score_matrix(1.6, 1.1, 10)
        assert abs(sum(sum(r) for r in m) - 1.0) < 1e-9

    def test_matrix_normalized_with_rho(self):
        m, _, _ = score_matrix(1.6, 1.1, 10, rho=-0.1)
        assert abs(sum(sum(r) for r in m) - 1.0) < 1e-9

    def test_dc_tau_values(self):
        assert dc_tau(0, 0, 1.5, 1.0, -0.1) == pytest.approx(1.0 - 1.5 * 1.0 * -0.1)
        assert dc_tau(1, 1, 1.5, 1.0, -0.1) == pytest.approx(1.0 + 0.1)
        assert dc_tau(2, 3, 1.5, 1.0, -0.1) == 1.0

    def test_lambda_clamped(self):
        m, lh, la = score_matrix(99.0, 0.001, 10)
        assert lh <= 4.5 and la >= 0.08

    def test_markets_sums(self):
        m, _, _ = score_matrix(1.5, 1.2, 10)
        mk = markets_from_matrix(m)
        assert mk["1"] + mk["N"] + mk["2"] == pytest.approx(1.0, abs=1e-9)
        assert mk["O2.5"] + mk["U2.5"] == pytest.approx(1.0)
        assert mk["BTTS_YES"] + mk["BTTS_NO"] == pytest.approx(1.0)
        assert mk["O1.5"] >= mk["O2.5"] >= mk["O3.5"]
        assert len(mk["top_scores"]) == 5

    def test_stronger_home_favored(self):
        m, _, _ = score_matrix(2.4, 0.8, 10)
        mk = markets_from_matrix(m)
        assert mk["1"] > mk["2"]


class TestGuards:
    def test_data_levels(self):
        assert data_level(12, 10) == FULL_DATA
        assert data_level(2, 5) == LOW_DATA
        assert data_level(1, 0) == INSUFFICIENT_DATA

    def test_no_prediction_on_insufficient(self):
        assert prediction_allowed(INSUFFICIENT_DATA) is False
        assert prediction_allowed("FULL_DATA") is True

    def test_clamp_prob_bounds(self):
        assert clamp_prob(1.5) == 0.99
        assert clamp_prob(-0.2) == 0.01

    def test_extreme_needs_support(self):
        # 98 % sans support → ramené à 0.90 exactement (pas de 98 % gratuit)
        assert publish_guard(0.98, 0) == pytest.approx(0.90)
        # support partiel → interpolation
        v = publish_guard(0.98, 25)
        assert 0.90 < v < 0.98
        # support suffisant → valeur conservée
        assert publish_guard(0.98, 200) == pytest.approx(0.98, abs=0.011)

    def test_extreme_low_mirror(self):
        assert publish_guard(0.005, 0) == pytest.approx(0.10)

    def test_interior_untouched(self):
        assert publish_guard(0.62, 0) == pytest.approx(0.62)

    def test_100_percent_never_returned_without_support(self):
        assert publish_guard(1.0, 0) <= 0.99
        assert publish_guard(1.0, 0) < 1.0

    def test_metric_sample_verdicts(self):
        assert metric_verdict(10) == "INSUFFICIENT_SAMPLE"
        assert metric_verdict(50) == "WEAK_SAMPLE"
        assert metric_verdict(500) == "OK"


class TestModels:
    def _feats(self):
        return {"league_home_avg": 1.5, "league_away_avg": 1.2,
                "att_home": 1.3, "def_home": 0.9,
                "att_away": 0.8, "def_away": 1.15,
                "home_matches": 12, "away_matches": 11}

    def test_A_uses_only_league_avgs(self):
        p = Model2C_A().predict(self._feats())
        assert p["lambda_h"] == pytest.approx(1.5)
        assert p["lambda_a"] == pytest.approx(1.2)
        mk = p["markets"]
        assert mk["1"] + mk["N"] + mk["2"] == pytest.approx(1.0)

    def test_B_combines_strengths(self):
        p = Model2C_B().predict(self._feats())
        assert p["lambda_h"] == pytest.approx(1.5 * 1.3 * 1.15)
        assert p["lambda_a"] == pytest.approx(1.2 * 0.8 * 0.9)
        mk = p["markets"]
        assert mk["1"] > mk["2"]

    def test_C_none_without_params(self):
        assert Model2C_C(None).predict("A", "B") is None
        assert Model2C_C(None).available is False

    def test_C_with_params(self):
        params = DixonColesParams(["A", "B"], [0.4, -0.4], [0.1, -0.1], 0.25, -0.1,
                                  0.0065, 500, False)
        p = Model2C_C(params).predict("A", "B")
        assert p and p["lambda_h"] > p["lambda_a"]
        assert p["xg_used"] is False and p["n_train"] == 500

    def test_C_unknown_team_gets_league_average(self):
        params = DixonColesParams(["A", "B"], [0.4, -0.4], [0.1, -0.1], 0.25, 0.0,
                                  0.0065, 500, False)
        p = Model2C_C(params).predict("NOUVEAU", "B")
        assert p is not None  # inconnu → params 0 (moyenne), pas de crash

    def test_D_requires_C(self):
        d = Model2C_D(0.5)
        b = Model2C_B().predict(self._feats())
        assert d.predict(b, None, "H", "A") is None

    def test_D_mixture_normalized(self):
        b = Model2C_B().predict(self._feats())
        params = DixonColesParams(["H", "A"], [0.2, -0.2], [0.1, -0.1], 0.25, -0.05,
                                  0.0065, 300, False)
        c = Model2C_C(params).predict("H", "A")
        d = Model2C_D(0.5).predict(b, c, "H", "A")
        mk = d["markets"]
        assert mk["1"] + mk["N"] + mk["2"] == pytest.approx(1.0, abs=1e-9)
        assert d["weight_c"] == 0.5

    def test_predict_all_insufficient(self):
        out = predict_all({"home_matches": 0, "away_matches": 1}, "X", "Y", None)
        assert out["status"] == NO_PREDICTION
        assert "INSUFFICIENT" in out["reason"]

    def test_predict_all_ok(self):
        out = predict_all(self._feats(), "H", "A", None)
        assert out["status"] == "OK"
        assert out["data_quality"]["label"].startswith("DATA_QUALITY")
        assert out["models"]["A"] and out["models"]["B"]
        assert out["models"]["C"] is None
