# -*- coding: utf-8 -*-
"""2C §2 — ANTI-LEAKAGE P0 : tests de rejet systématique des données futures."""
import pytest
from datetime import timezone
from model2c.availability import (is_available_at, available_points,
                                  assert_no_future, parse_ts,
                                  make_effective_from_kickoff, LIVE, BACKTEST)

T = "2026-06-01T18:00:00+00:00"


def _dp(eff=None, ret=None):
    d = {}
    if eff is not None:
        d["effective_at"] = eff
    if ret is not None:
        d["retrieved_at"] = ret
    return d


class TestModes:
    def test_live_accepts_past_both_clocks(self):
        assert is_available_at(_dp("2026-06-01T17:00:00+00:00",
                                   "2026-06-01T17:01:00+00:00"), T, LIVE) is True

    def test_live_rejects_future_effective(self):
        assert is_available_at(_dp("2026-06-01T18:00:01+00:00",
                                   "2026-06-01T17:00:00+00:00"), T, LIVE) is False

    def test_live_rejects_future_retrieved(self):
        assert is_available_at(_dp("2026-06-01T17:00:00+00:00",
                                   "2026-06-01T18:30:00+00:00"), T, LIVE) is False

    def test_backtest_rejects_future_effective(self):
        assert is_available_at(_dp("2026-06-01T23:59:00+00:00"), T, BACKTEST) is False

    def test_backtest_ignores_retrieval_artifact(self):
        # un fichier historique téléchargé aujourd'hui reste utilisable pour
        # reconstituer le passé : seule effective_at compte (documenté §2)
        assert is_available_at(_dp("2021-05-01T19:30:00+00:00",
                                   "2026-09-05T20:00:00+00:00"),
                               "2021-05-02T00:00:00+00:00", BACKTEST) is True
        # MAIS en mode live, la même donnée est refusée (non recueillie à T)
        assert is_available_at(_dp("2021-05-01T19:30:00+00:00",
                                   "2026-09-05T20:00:00+00:00"),
                               "2021-05-02T00:00:00+00:00", LIVE) is False

    def test_effective_exactly_at_T_is_allowed(self):
        assert is_available_at(_dp(T, T), T, LIVE) is True

    def test_missing_effective_refused(self):
        assert is_available_at(_dp(ret="2026-06-01T17:00:00+00:00"), T, LIVE) is False
        assert is_available_at(_dp(ret="2026-06-01T17:00:00+00:00"), T, BACKTEST) is False

    def test_missing_retrieved_refused_live(self):
        assert is_available_at(_dp(eff="2026-06-01T17:00:00+00:00"), T, LIVE) is False

    def test_empty_datapoint_refused(self):
        assert is_available_at({}, T, LIVE) is False
        assert is_available_at({}, T, BACKTEST) is False

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            is_available_at(_dp(T, T), T, "shuffle")

    def test_invalid_T_raises(self):
        with pytest.raises(ValueError):
            is_available_at(_dp(T, T), None, LIVE)


class TestBatchAndGuards:
    def test_available_points_filters_future(self):
        pts = [_dp("2026-06-01T17:00:00+00:00", "2026-06-01T17:00:00+00:00"),
               _dp("2026-06-02T10:00:00+00:00", "2026-06-02T11:00:00+00:00"),
               _dp("2026-06-01T12:00:00+00:00", "2026-06-01T12:05:00+00:00")]
        kept = available_points(pts, T, LIVE)
        assert len(kept) == 2
        assert all(is_available_at(p, T, LIVE) for p in kept)

    def test_assert_no_future_raises_on_leak(self):
        pts = [_dp("2026-06-03T00:00:00+00:00", "2026-06-03T00:01:00+00:00")]
        with pytest.raises(AssertionError):
            assert_no_future(pts, T, LIVE)

    def test_assert_no_future_passes_clean(self):
        pts = [_dp("2026-05-01T00:00:00+00:00", "2026-05-01T00:01:00+00:00")]
        assert assert_no_future(pts, T, LIVE) is True

    def test_future_in_backtest_also_raises(self):
        pts = [_dp("2030-01-01T00:00:00+00:00")]
        with pytest.raises(AssertionError):
            assert_no_future(pts, T, BACKTEST)


class TestTimestamps:
    def test_parse_z_suffix(self):
        dt = parse_ts("2026-06-01T18:00:00Z")
        assert dt.tzinfo == timezone.utc and dt.hour == 18

    def test_parse_naive_becomes_utc(self):
        dt = parse_ts("2026-06-01T18:00:00")
        assert dt.tzinfo == timezone.utc

    def test_parse_none(self):
        assert parse_ts(None) is None

    def test_effective_from_kickoff(self):
        eff = make_effective_from_kickoff("2026-06-01T18:30:00Z", 2.0)
        assert parse_ts(eff).hour == 20 and parse_ts(eff).minute == 30
