# -*- coding: utf-8 -*-
"""2C §3 — ingestion : parsing RÉEL (fixtures de vraies données), horloges PIT."""
import json
import os
import pytest

from model2c.ingest_history import (parse_ol_season, aggregate_xg_from_events)
from model2c.availability import parse_ts, is_available_at

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "2c")
RET = "2026-09-05T20:00:00+00:00"


def _ol():
    return json.load(open(os.path.join(FIX, "ol_bl_sample.json"), encoding="utf-8"))


class TestOpenLigaDBParse:
    def test_canonical_records(self):
        out = parse_ol_season(_ol(), "bl1", 2024, RET)
        assert len(out) == 12
        m = out[0]
        assert m["match_id"].startswith("ol:bl1:")
        assert m["league"] == "bl1" and m["season"] == 2024
        assert m["hg"] is not None and m["ag"] is not None
        assert m["source"] == "openligadb"

    def test_first_match_real_score(self):
        # Vraie donnée : Gladbach 2-3 Leverkusen, 2024-08-23 18:30Z (sonde réelle)
        out = parse_ol_season(_ol(), "bl1", 2024, RET)
        m = out[0]
        assert (m["hg"], m["ag"]) == (2, 3)
        assert m["home"] == "borussia monchengladbach"
        assert m["away"] == "bayer leverkusen"

    def test_clocks_present_and_ordered(self):
        out = parse_ol_season(_ol(), "bl1", 2024, RET)
        for m in out:
            ko = parse_ts(m["kickoff_utc"])
            eff = parse_ts(m["effective_at"])
            assert eff > ko                       # résultat disponible APRÈS le match
            assert (eff - ko).total_seconds() == 2 * 3600
            assert m["retrieved_at"] == RET

    def test_backtest_availability_semantics(self):
        out = parse_ol_season(_ol(), "bl1", 2024, RET)
        m = out[0]
        day_after = "2024-08-24T12:00:00+00:00"
        # backtest : le résultat du 23/08 était publiquement disponible le 24/08
        assert is_available_at({"effective_at": m["effective_at"]}, day_after, "backtest")
        # live : NOTRE récolte est plus tardive → refus honnête
        assert not is_available_at({"effective_at": m["effective_at"],
                                    "retrieved_at": m["retrieved_at"]}, day_after, "live")

    def test_unfinished_matches_skipped(self):
        payload = _ol()[:2] + [{"matchIsFinished": False, "matchID": 1,
                                "matchDateTimeUTC": "2024-09-01T15:30:00Z",
                                "team1": {"teamName": "X"}, "team2": {"teamName": "Y"},
                                "matchResults": []}]
        out = parse_ol_season(payload, "bl1", 2024, RET)
        assert len(out) == 2

    def test_no_score_invented_when_final_missing(self):
        payload = [{"matchIsFinished": True, "matchID": 2,
                    "matchDateTimeUTC": "2024-09-01T15:30:00Z",
                    "team1": {"teamName": "X"}, "team2": {"teamName": "Y"},
                    "matchResults": [{"resultTypeID": 1, "pointsTeam1": 1, "pointsTeam2": 0}]}]
        assert parse_ol_season(payload, "bl1", 2024, RET) == []

    def test_same_team_rejected(self):
        payload = [{"matchIsFinished": True, "matchID": 3,
                    "matchDateTimeUTC": "2024-09-01T15:30:00Z",
                    "team1": {"teamName": "FC Bayern München"},
                    "team2": {"teamName": "Bayern Munich"},
                    "matchResults": [{"resultTypeID": 2, "pointsTeam1": 1, "pointsTeam2": 0}]}]
        assert parse_ol_season(payload, "bl1", 2024, RET) == []

    def test_chronological_sort_possible(self):
        out = parse_ol_season(_ol(), "bl1", 2024, RET)
        ks = [parse_ts(m["kickoff_utc"]) for m in sorted(out, key=lambda x: x["kickoff_utc"])]
        assert ks == sorted(ks)


class TestStatsBombXg:
    def test_aggregate_real(self):
        events = json.load(open(os.path.join(FIX, "sb_events_sample.json"), encoding="utf-8"))
        agg = aggregate_xg_from_events(events)
        assert agg["Union Berlin"] == pytest.approx(0.38)
        assert agg["Bayer Leverkusen"] == pytest.approx(0.52)  # tir sans xG ignoré

    def test_no_xg_invented(self):
        assert aggregate_xg_from_events([]) == {}
        ev = [{"type": {"name": "Pass"}, "team": {"name": "X"}}]
        assert aggregate_xg_from_events(ev) == {}

    def test_missing_xg_value_skipped(self):
        ev = [{"type": {"name": "Shot"}, "team": {"name": "X"}, "shot": {}}]
        assert aggregate_xg_from_events(ev) == {}
