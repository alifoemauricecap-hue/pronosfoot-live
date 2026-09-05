# -*- coding: utf-8 -*-
"""
WALK-FORWARD VALIDATION (§16) — OBLIGATOIRE
============================================
INTERDIT : random split / shuffle / train-test aléatoire.
Séparation TEMPORELLE stricte : train effective_end < test kickoff,
fenêtre d'entraînement EXPANSIVE par saison.

Chaque pli : Fold(fold_id, train, test) avec assertions anti-fuite
intégrées (P0) : un pli dont le train touche le futur du test LÈVE
une erreur — jamais de leak silencieux.
"""

from .availability import parse_ts
from .config2c import CONFIG_2C as C


class Fold:
    def __init__(self, fold_id, train, test):
        self.fold_id = fold_id
        self.train = list(train)
        self.test = list(test)
        self._assert_temporal()

    def _assert_temporal(self):
        if not self.train or not self.test:
            return
        max_train = max(parse_ts(m["kickoff_utc"]) for m in self.train)
        min_test = min(parse_ts(m["kickoff_utc"]) for m in self.test)
        assert max_train < min_test, (
            f"TEMPORAL LEAK pli {self.fold_id} : train max {max_train} >= test min {min_test}")


def by_season_folds(matches, test_seasons=None, min_train=None):
    """matches : liste chronologique avec 'season' (année de début) et
    'kickoff_utc'. Un pli par saison de test ; train = tout le passé strict."""
    test_seasons = test_seasons or C["wf_test_seasons"]
    min_train = min_train or C["wf_min_train_matches"]
    folds = []
    for s in test_seasons:
        train = [m for m in matches if m["season"] < s]
        test = [m for m in matches if m["season"] == s]
        if len(train) < min_train or not test:
            continue  # pli honnêtement ignoré (données), JAMAIS truqué
        folds.append(Fold(f"test_season_{s}", train, test))
    return folds
