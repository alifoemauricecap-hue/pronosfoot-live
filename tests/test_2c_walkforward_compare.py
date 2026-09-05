# -*- coding: utf-8 -*-
"""2C §16/§26 — séparation temporelle stricte + verdicts du comparateur."""
import pytest
from datetime import datetime, timezone, timedelta

from model2c.walkforward import Fold, by_season_folds
from model2c.compare import compare

T0 = datetime(2020, 8, 1, 15, 30, tzinfo=timezone.utc)


def _m(i, season, day_offset):
    return {"match_id": f"m{i}", "season": season, "league": "bl1",
            "kickoff_utc": (T0 + timedelta(days=int(day_offset))).isoformat(),
            "home": f"H{i % 9}", "away": f"A{i % 9}", "hg": 1, "ag": 0,
            "effective_at": (T0 + timedelta(days=int(day_offset), hours=2)).isoformat()}


class TestFolds:
    def test_no_random_split_possible(self):
        train = [_m(1, 2018, 0)]
        test = [_m(2, 2019, -30)]  # test AVANT le train → leak
        with pytest.raises(AssertionError):
            Fold("leak", train, test)

    def test_valid_fold(self):
        f = Fold("ok", [_m(1, 2018, 0)], [_m(2, 2019, 10)])
        assert f.fold_id == "ok"

    def test_by_season_folds(self):
        matches = ([_m(i, 2017, i) for i in range(60)]
                   + [_m(100 + i, 2018, 400 + i) for i in range(60)]
                   + [_m(200 + i, 2019, 800 + i) for i in range(30)])
        folds = by_season_folds(matches, test_seasons=[2018, 2019], min_train=20)
        assert len(folds) == 2
        assert folds[0].fold_id == "test_season_2018"
        assert all(m["season"] < 2018 for m in folds[0].train)
        assert all(m["season"] == 2018 for m in folds[0].test)

    def test_insufficient_train_fold_skipped_honestly(self):
        matches = [_m(i, 2019, i) for i in range(10)]
        folds = by_season_folds(matches, test_seasons=[2019], min_train=50)
        assert folds == []

    def test_expanding_window(self):
        matches = ([_m(i, 2017, i) for i in range(50)]
                   + [_m(100 + i, 2018, 400 + i) for i in range(50)]
                   + [_m(200 + i, 2019, 800 + i) for i in range(20)])
        f18, f19 = by_season_folds(matches, test_seasons=[2018, 2019], min_train=10)
        assert len(f19.train) > len(f18.train)  # fenêtre expansive


class TestCompare:
    GOOD = [{"1": 0.72, "N": 0.16, "2": 0.12}] * 150
    FLAT = [{"1": 0.34, "N": 0.33, "2": 0.33}] * 150
    OUTS = ["1"] * 150

    def test_better(self):
        r = compare(self.FLAT, self.GOOD, self.OUTS)
        assert r["verdict"] == "2C meilleur"
        assert r["delta_logloss"]["delta"] > 0

    def test_worse(self):
        r = compare(self.GOOD, self.FLAT, self.OUTS)
        assert r["verdict"] == "2C pire"

    def test_equivalent_large_n(self):
        r = compare(self.GOOD, [dict(d) for d in self.GOOD], self.OUTS)
        assert r["verdict"] == "2C équivalent"

    def test_insufficient_sample(self):
        r = compare(self.FLAT[:10], self.GOOD[:10], self.OUTS[:10])
        assert r["verdict"] == "2C échantillon insuffisant"

    def test_bundles_present(self):
        r = compare(self.FLAT, self.GOOD, self.OUTS)
        assert r["2A"]["n"] == 150 and r["2C"]["n"] == 150
        assert "eci" not in r  # sanity
        assert "brier" in r["2C"] and "logloss" in r["2A"]

    def test_pairs_on_same_matches(self):
        # deltas appariés : mêmes outcomes des deux côtés (structure imposée)
        r = compare(self.FLAT, self.GOOD, self.OUTS)
        assert r["delta_brier"]["n"] == 150
