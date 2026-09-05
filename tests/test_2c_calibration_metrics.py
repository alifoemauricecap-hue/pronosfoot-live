# -*- coding: utf-8 -*-
"""2C §17-§20 — calibration (jamais sur le test), métriques avec N, IC."""
import pytest
from model2c.calibration import PlattCalibrator, IsotonicCalibrator, MulticlassCalibrator
from model2c.metrics import (brier_multi, logloss_multi, accuracy_argmax,
                             ece_binary, reliability_table, metric_bundle,
                             paired_bootstrap_ci, multiclass_view)


class TestPlatt:
    def test_untrained_passthrough_clamped(self):
        c = PlattCalibrator()
        assert c.predict(0.7) == pytest.approx(0.7)
        assert c.predict(1.5) == 0.99

    def test_min_pairs_required(self):
        c = PlattCalibrator().fit([0.6] * 10, [1] * 10)
        assert c.trained is False

    def test_fit_pulls_toward_observed(self):
        raw = [0.9] * 60 + [0.5] * 60
        out = [1] * 42 + [0] * 18 + [1] * 30 + [0] * 30
        c = PlattCalibrator().fit(raw, out)
        assert c.trained
        p09 = c.predict(0.9)
        # 42/60 = 0.70 observé → la calibration tire 0.9 fortement vers le bas
        assert p09 < 0.9

    def test_output_bounded(self):
        c = PlattCalibrator().fit([0.95] * 60, [1] * 60)
        assert 0.01 <= c.predict(0.999) <= 0.99


class TestIsotonic:
    def test_min_pairs_required(self):
        c = IsotonicCalibrator().fit([0.5] * 5, [1] * 5)
        assert c.trained is False
        assert c.predict(0.5) == pytest.approx(0.5)

    def test_monotonic_non_decreasing(self):
        import random
        rng = random.Random(7)
        raw = [rng.random() for _ in range(200)]
        out = [1 if rng.random() < p else 0 for p in raw]
        c = IsotonicCalibrator().fit(raw, out)
        assert c.trained
        xs = sorted(raw)[:50]
        preds = [c.predict(x) for x in xs]
        assert all(b >= a - 1e-9 for a, b in zip(preds, preds[1:]))

    def test_output_bounded(self):
        c = IsotonicCalibrator().fit([0.1] * 40 + [0.9] * 40, [0] * 40 + [1] * 40)
        assert 0.01 <= c.predict(0.0) <= c.predict(1.0) <= 0.99


class TestMulticlass:
    def test_renormalized_sum_one(self):
        dists = [{"1": 0.7, "N": 0.2, "2": 0.1}] * 50
        outs = ["1"] * 30 + ["N"] * 10 + ["2"] * 10
        cal = MulticlassCalibrator("platt").fit(dists, outs)
        p = cal.predict(dists[0])
        assert p["1"] + p["N"] + p["2"] == pytest.approx(1.0)

    def test_isotonic_variant(self):
        dists = [{"1": 0.6, "N": 0.25, "2": 0.15}] * 60
        outs = ["1"] * 40 + ["N"] * 12 + ["2"] * 8
        cal = MulticlassCalibrator("isotonic").fit(dists, outs)
        p = cal.predict(dists[0])
        assert abs(sum(p.values()) - 1.0) < 1e-9


class TestMetrics:
    def test_brier_known_values(self):
        assert brier_multi({"1": 1.0, "N": 0.0, "2": 0.0}, "1") == pytest.approx(0.0)
        assert brier_multi({"1": 0.0, "N": 0.0, "2": 1.0}, "1") == pytest.approx(2.0)
        uniform = {"1": 1 / 3, "N": 1 / 3, "2": 1 / 3}
        assert brier_multi(uniform, "1") == pytest.approx((2 / 3) ** 2 + 2 * (1 / 3) ** 2)

    def test_logloss_known(self):
        assert logloss_multi({"1": 1.0}, "1") == pytest.approx(0.0)
        assert logloss_multi({"1": 0.5}, "1") == pytest.approx(0.6931471805599453)

    def test_accuracy(self):
        assert accuracy_argmax({"1": 0.7, "N": 0.2, "2": 0.1}, "1") == 1
        assert accuracy_argmax({"1": 0.7, "N": 0.2, "2": 0.1}, "2") == 0

    def test_ece_perfect_low(self):
        probs = [0.0] * 50 + [1.0] * 50
        outs = [0] * 50 + [1] * 50
        assert ece_binary(probs, outs, bins=5) == pytest.approx(0.0)

    def test_ece_worst_high(self):
        probs = [0.0] * 50 + [1.0] * 50
        outs = [1] * 50 + [0] * 50
        assert ece_binary(probs, outs, bins=5) == pytest.approx(1.0)

    def test_reliability_table_counts(self):
        probs = [0.05, 0.15, 0.85]
        rows = reliability_table(probs, [0, 0, 1], bins=10)
        assert sum(r["n"] for r in rows) == 3

    def test_bundle_has_n_and_verdict(self):
        dists = [{"1": 0.6, "N": 0.3, "2": 0.1}] * 40
        outs = ["1"] * 25 + ["N"] * 10 + ["2"] * 5
        b = metric_bundle(dists, outs, "t")
        assert b["n"] == 40
        assert b["sample_verdict"] == "WEAK_SAMPLE"
        assert "brier" in b and "logloss" in b and "ece" in b and "reliability" in b

    def test_bundle_empty(self):
        assert metric_bundle([], [], "t")["sample_verdict"] == "INSUFFICIENT_SAMPLE"

    def test_bootstrap_insufficient(self):
        r = paired_bootstrap_ci([{"1": 0.5}] * 10, [{"1": 0.4}] * 10, ["1"] * 10)
        assert r["verdict"] == "INSUFFICIENT_SAMPLE"

    def test_bootstrap_detects_better(self):
        good = [{"1": 0.75, "N": 0.15, "2": 0.10}] * 120
        bad = [{"1": 0.34, "N": 0.33, "2": 0.33}] * 120
        outs = ["1"] * 120
        r = paired_bootstrap_ci(bad, good, outs, stat="logloss")
        assert r["significant"] and r["delta"] > 0   # stat(bad) - stat(good) > 0

    def test_bootstrap_deterministic_seed(self):
        d = [{"1": 0.55, "N": 0.25, "2": 0.2}] * 60
        o = ["1"] * 33 + ["N"] * 15 + ["2"] * 12
        r1 = paired_bootstrap_ci(d, d, o)
        r2 = paired_bootstrap_ci(d, d, o)
        assert r1["ci95"] == r2["ci95"]

    def test_multiclass_view(self):
        probs, outs = multiclass_view([{"1": 0.7, "N": 0.2, "2": 0.1}], ["1"])
        assert probs == [0.7] and outs == [1]
