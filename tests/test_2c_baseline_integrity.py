# -*- coding: utf-8 -*-
"""2C — INTÉGRITÉ DE LA BASELINE 2A : les fichiers 2A sont BIT À BIT IDENTIQUES.
Toute modification de la baseline casse immédiatement ces tests (alarme voulue)."""
import hashlib
import os
import pytest
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PINNED = {
    "engine.py": "be774af3b35237a6e566ddd58429c0d89bee013b935cfc034e5883dd4edf3a1e",
    "prediction_service.py": "4e7f2c5f7a245d4e8c7f8a0878860a68ddcca263c4a6654db19e5280ce6167f0",
    "app.py": "d7868fd627eb9ba3c816dca90c85a18be756051c31c01514014ef00362957bad",
    "repository.py": "206fc4bd48b872fa78660a4f84354a51e3b3c2e011df6f1ba8b83e3803da4947",
    "static/index.html": "ef984f3bbc84aae695b5edb6850a323500915062819feec60a1ec55d6c9066d4",
    "sources/registry.json": "c014e1da9faaa0450ef516c32e8caa5d6e216952ab2d5cf0a5a4d33b38dfa3f8",
}


@pytest.mark.parametrize("rel", sorted(PINNED))
def test_baseline_file_unmodified(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        h = hashlib.sha256(f.read()).hexdigest()
    assert h == PINNED[rel], f"FICHIER 2A MODIFIÉ : {rel} — baseline non immutable ?"


T0 = datetime(2025, 3, 1, 15, 30, tzinfo=timezone.utc)


def _match(i, d_ago, h, a, hg, ag):
    ko = T0 - timedelta(days=d_ago)
    return {"match_id": f"t{i}", "league": "t", "season": 2024,
            "kickoff_utc": ko.isoformat(), "home": h, "away": a,
            "hg": hg, "ag": ag}


class TestReplay2AFaithfulness:
    """Le rejeu doit reproduire la sémantique EXACTE de app.build_stats :
    w = 0.5**(age_jours/70), fenêtre 150 j, clamps ligue [0.9,2.3]/[0.7,2.0]."""

    def test_weights_and_league_avgs_handcomputed(self):
        from model2c.replay2a import build_stats_at
        ms = [_match(1, 10, "H", "A", 3, 0),
              _match(2, 1, "H", "B", 1, 1),
              _match(3, 1, "B", "A", 0, 2)]
        s = build_stats_at(ms, T0)
        w10, w1 = 0.5 ** (10 / 70.0), 0.5 ** (1 / 70.0)
        th = s["teams"]["H"]
        assert th["sw"] == pytest.approx(w10 + w1, rel=1e-9)
        assert th["gf"] == pytest.approx(3 * w10 + 1 * w1, rel=1e-9)
        assert th["home_sw"] == pytest.approx(w10 + w1, rel=1e-9)
        lg = s["league"]
        exp_home = (3 * w10 + 1 * w1 + 0 * w1) / (w10 + w1 + w1)
        exp_away = (0 * w10 + 1 * w1 + 2 * w1) / (w10 + w1 + w1)
        assert lg["homeAvg"] == pytest.approx(min(max(exp_home, 0.9), 2.3), rel=1e-9)
        assert lg["awayAvg"] == pytest.approx(min(max(exp_away, 0.7), 2.0), rel=1e-9)

    def test_window_150_days(self):
        from model2c.replay2a import build_stats_at
        ms = [_match(1, 200, "H", "A", 5, 5),   # hors fenêtre → ignoré
              _match(2, 5, "H", "A", 1, 0)]
        s = build_stats_at(ms, T0)
        assert s["teams"]["H"]["n"] == 1
        assert s["league"]["n"] == 1

    def test_clamp_applies(self):
        from model2c.replay2a import build_stats_at
        ms = [_match(1, 3, "H", "A", 9, 0),
              _match(2, 2, "C", "D", 8, 0)]
        lg = build_stats_at(ms, T0)["league"]
        assert lg["homeAvg"] <= 2.3 and lg["awayAvg"] >= 0.7

    def test_predict_not_none_and_sums(self):
        from model2c.replay2a import predict_2a
        ms = []
        for wk in range(12):
            for i, (h, a) in enumerate((("H", "A"), ("H", "B"), ("A", "C"), ("B", "C"))):
                ms.append(_match(wk * 4 + i, 100 - wk * 4, h, a, 1 + (wk % 2), wk % 3))
        r = predict_2a(ms, "H", "A", T0)
        assert r is not None
        assert r["dist"]["1"] + r["dist"]["N"] + r["dist"]["2"] == pytest.approx(1.0, abs=1e-6)

    def test_unknown_teams_give_none_honest(self):
        from model2c.replay2a import predict_2a
        assert predict_2a([], "H", "A", T0) is None

    def test_future_matches_never_enter(self):
        from model2c.replay2a import build_stats_at
        past = [_match(1, 5, "H", "A", 1, 0)]
        s1 = build_stats_at(past, T0)
        s2 = build_stats_at(past + [{"match_id": "f", "league": "t", "season": 2024,
                                     "kickoff_utc": (T0 + timedelta(days=20)).isoformat(),
                                     "home": "H", "away": "A", "hg": 9, "ag": 9}], T0)
        assert s1["teams"]["H"]["gf"] == s2["teams"]["H"]["gf"]
        assert s1["league"]["n"] == s2["league"]["n"]
