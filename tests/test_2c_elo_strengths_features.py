# -*- coding: utf-8 -*-
"""2C §4-§7/§9-§11 — Elo, forces, forme, calendrier, xG UNKNOWN, anti-leak."""
import pytest
from datetime import datetime, timezone, timedelta

from model2c.elo import TemporalElo, build_ratings_through_time
from model2c.strengths import OnlineStrengths
from model2c.features import (TeamHistory, form_features, calendar_features,
                              opponent_strength, feature_hash,
                              build_match_features, FEATURE_REGISTRY)
from model2c.identity import canonical_team, norm_name

T0 = datetime(2025, 1, 10, 18, 30, tzinfo=timezone.utc)


def _mk(team, days_ago, gf, ga, loc="H", opp="X"):
    return (team, T0 - timedelta(days=days_ago), gf, ga, loc, opp)


class TestElo:
    def test_equal_teams_expected_half(self):
        e = TemporalElo()
        r, a = e.pre_match("A", "B")["elo_home"], None
        eh = e.pre_match("A", "B")["elo_expect_home"]
        assert eh > 0.5 and eh < 0.65  # avantage domicile seul

    def test_win_moves_rating_up(self):
        e = TemporalElo(k=24)
        before = e.rating("A")
        e.apply_result("A", "B", 3, 0)
        assert e.rating("A") > before
        assert e.rating("B") < 1500 - (e.rating("A") - before)  # proche zero-sum

    def test_bigger_k_bigger_move(self):
        e1, e2 = TemporalElo(k=16), TemporalElo(k=48)
        e1.apply_result("A", "B", 2, 1)
        e2.apply_result("A", "B", 2, 1)
        assert abs(e2.rating("A") - 1450) > abs(e1.rating("A") - 1450)

    def test_new_team_base(self):
        e = TemporalElo()
        assert e.rating("PROMU") == 1450.0

    def test_strictly_temporal_no_future(self):
        """Le rating à T ne dépend JAMAIS d'un match futur (P0)."""
        ms = [{"match_id": str(i), "home": "A", "away": "B", "hg": 5, "ag": 0,
               "kickoff_utc": (T0 + timedelta(days=i)).isoformat()} for i in range(5)]
        past_only = ms[:2]
        h1 = build_ratings_through_time(past_only, k=24)
        h2 = build_ratings_through_time(ms, k=24)
        # les features des 2 premiers matchs doivent être IDENTIQUES, futur ou pas
        assert h1[0][1] == h2[0][1]
        assert h1[1][1] == h2[1][1]

    def test_margin_multiplier(self):
        e1, e2 = TemporalElo(margin_mult=True), TemporalElo(margin_mult=False)
        e1.apply_result("A", "B", 6, 0)
        e2.apply_result("A", "B", 6, 0)
        assert e1.rating("A") > e2.rating("A")


class TestStrengths:
    def _feed(self, st, team, other, n, gf, ga, loc_h=True):
        for i in range(n):
            d = (T0 - timedelta(days=40 - i)).isoformat()
            ko = (T0 - timedelta(days=40 - i, hours=2)).isoformat()
            if loc_h:
                st.update(team, other, gf, ga, ko, d)
            else:
                st.update(other, team, ga, gf, ko, d)

    def test_no_data_prior_one(self):
        st = OnlineStrengths()
        f = st.features_pre("X", "Y", T0.isoformat(), None, None)
        assert f["att_home"] == 1.0 and f["def_away"] == 1.0
        assert f["home_matches"] == 0

    def test_shrinkage_small_sample(self):
        st = OnlineStrengths()
        # 1 match 6-0 : la shrinkage DOIT ramener vers la moyenne
        self._feed(st, "H", "A", 1, 6, 0)
        f = st.features_pre("H", "A", T0.isoformat(), None, None)
        raw = 6.0 / f["league_home_avg"]
        assert 1.0 < f["att_home"] < raw
        assert f["home_shrinkage_applied"] is True

    def test_many_matches_less_shrinkage(self):
        st = OnlineStrengths()
        self._feed(st, "H", "A", 12, 2, 1)
        # ligue remplie par d'autres matchs
        for i in range(12):
            st.update(f"L{i}", f"M{i}", 1, 1, (T0 - timedelta(days=39 - i)).isoformat(),
                      (T0 - timedelta(days=39 - i)).isoformat())
        f = st.features_pre("H", "A", T0.isoformat(), None, None)
        assert f["att_home"] > 1.0
        assert f["home_matches"] == 12

    def test_elo_prior_fades_with_matches(self):
        st = OnlineStrengths()
        f0 = st.features_pre("H", "A", T0.isoformat(), 1700, 1300)
        self._feed(st, "H", "A", 8, 1, 1)
        f8 = st.features_pre("H", "A", T0.isoformat(), 1700, 1300)
        # après 8 matchs, l'élo n'a plus de poids (anti double comptage)
        assert "att_home" in f8
        ratio0 = min(1.6, (1700 / 1500) ** 0.5)
        assert abs(f0["att_home"] - ratio0) < 0.01

    def test_xg_unknown_when_absent(self):
        st = OnlineStrengths()
        self._feed(st, "H", "A", 5, 2, 1)
        f = st.features_pre("H", "A", T0.isoformat(), None, None, xg_available=True)
        assert f["xg_att_home"] is None  # UNKNOWN, jamais 0.0

    def test_xg_real_when_present(self):
        st = OnlineStrengths()
        for i in range(4):
            ko = (T0 - timedelta(days=30 - i * 3)).isoformat()
            st.update("H", "A", 1, 0, ko, ko, xg_home=1.8, xg_away=0.6)
        f = st.features_pre("H", "A", T0.isoformat(), None, None, xg_available=True)
        assert f["xg_att_home"] is not None and f["xg_att_home"] > 1.0
        assert f["xg_matches_home"] == 4


class TestFormAndCalendar:
    def _hist(self, results):
        h = TeamHistory("H")
        for i, (gf, ga) in enumerate(results):
            h.add(T0 - timedelta(days=(len(results) - i) * 4), gf, ga, "H", "Opp")
        return h

    def test_form_windows_means(self):
        h = self._hist([(2, 0), (1, 1), (3, 1), (0, 2), (2, 2)])
        f = form_features(h, T0)
        assert f["form_n_w5"] == 5
        assert f["form_gf_w5"] == pytest.approx((2 + 1 + 3 + 0 + 2) / 5)
        assert f["form_pts_w3"] == pytest.approx((3 + 0 + 1) / 3)
        assert f["form_cs_w5"] == pytest.approx(1 / 5)
        assert f["form_fts_w5"] == pytest.approx(1 / 5)

    def test_form_unknown_when_too_empty(self):
        h = TeamHistory("H")
        f = form_features(h, T0)
        assert f["form_pts_w5"] is None  # UNKNOWN, jamais 0
        assert f["form_n_w5"] == 0

    def test_window_15_needs_depth(self):
        h = self._hist([(1, 0)] * 3)
        f = form_features(h, T0)
        assert f["form_n_w15"] == 3
        assert f["form_pts_w15"] is None  # profondeur insuffisante → honnête

    def test_calendar_rest_and_congestion(self):
        h = TeamHistory("H")
        h.add(T0 - timedelta(days=3), 1, 0, "H", "X")
        h.add(T0 - timedelta(days=6), 1, 0, "A", "Y")
        h.add(T0 - timedelta(days=20), 1, 0, "H", "Z")
        c = calendar_features(h, T0)
        assert c["rest_days"] == 3.0
        assert c["matches_7d"] == 2
        assert c["matches_14d"] == 2

    def test_calendar_empty_unknown_rest(self):
        c = calendar_features(TeamHistory("H"), T0)
        assert c["rest_days"] is None and c["matches_7d"] == 0

    def test_opponent_strength(self):
        h = TeamHistory("H")
        h.add(T0 - timedelta(days=2), 1, 0, "H", "X", opp_elo=1600)
        h.add(T0 - timedelta(days=5), 0, 1, "A", "Y", opp_elo=1400)
        assert opponent_strength(h, T0) == pytest.approx(1500.0)

    def test_only_past_matches_count(self):
        """Anti-fuite feature : un match du futur ne doit PAS entrer dans la forme."""
        h = TeamHistory("H")
        h.add(T0 + timedelta(days=7), 9, 9, "H", "FUTUR")   # futur → ignoré
        c = calendar_features(h, T0)
        f = form_features(h, T0)
        assert c["matches_7d"] == 0 and c["rest_days"] is None
        assert f["form_n_w3"] == 0


class TestFeatureRegistryAndHash:
    def test_registry_documents_unknowns(self):
        assert FEATURE_REGISTRY["weather"]["kind"] == "UNKNOWN"
        assert FEATURE_REGISTRY["injuries"]["kind"] == "UNKNOWN"

    def test_hash_deterministic(self):
        f = {"a": 1.0, "b": [1, 2]}
        assert feature_hash(f, T0) == feature_hash(f, T0)

    def test_hash_changes_with_value(self):
        assert feature_hash({"a": 1.0}, T0) != feature_hash({"a": 1.1}, T0)

    def test_hash_changes_with_as_of(self):
        assert feature_hash({"a": 1.0}, T0) != feature_hash(
            {"a": 1.0}, T0 + timedelta(days=1))

    def test_build_match_features_sources(self):
        f = build_match_features({"match_id": "m"}, T0, {"elo_home": 1500, "elo_away": 1450},
                                 {"league_home_avg": 1.5, "league_away_avg": 1.2},
                                 TeamHistory("H"), TeamHistory("A"))
        assert f["_sources"] == ["openligadb"]
        assert "feature_hash" in f
        assert f["_feature_version"]


class TestIdentity:
    def test_norm(self):
        assert norm_name("Borussia Mönchengladbach") == "borussia monchengladbach"

    def test_alias_cross_source(self):
        assert canonical_team("FC Bayern München") == "bayern munchen"
        assert canonical_team("Bayern Munich") == "bayern munchen"
        assert canonical_team("Union Berlin") == canonical_team("1. FC Union Berlin")

    def test_unknown_never_guessed(self):
        assert canonical_team("Fortuna Köln") is None  # ambigu déclaré
        assert canonical_team("") is None
