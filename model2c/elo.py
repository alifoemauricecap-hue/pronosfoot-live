# -*- coding: utf-8 -*-
"""
ELO TEMPOREL (§6)
=================
Rating calculé UNIQUEMENT avec les matchs TERMINÉS AVANT T.
- base 1500 ; équipe nouvelle/promue : elo_new_team_base (heuristique déclarée) ;
- avantage domicile ajouté au rating attendu ;
- K-factor configurable (testé sur plis de validation, JAMAIS sur le test) ;
- multiplicateur optionnel de différence de buts (sqrt, documenté).

Pre-match : history_for(match) fournit le rating STRICTEMENT AVANT le match.
Aucun match futur ne peut influencer un rating passé (test anti-fuite dédié).
"""

import math
from .availability import parse_ts


class TemporalElo:
    def __init__(self, k=24.0, home_adv=65.0, base=1500.0,
                 new_team_base=1450.0, margin_mult=True):
        self.k = float(k)
        self.home_adv = float(home_adv)
        self.base = float(base)
        self.new_team_base = float(new_team_base)
        self.margin_mult = bool(margin_mult)
        self.ratings = {}

    def rating(self, team):
        return self.ratings.get(team, self.new_team_base)

    def expected(self, ra, rb, home=True):
        """Probabilité attendue (1N2 sans nul au sens Elo : victoire)."""
        diff = (ra + (self.home_adv if home else 0.0)) - rb
        return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))

    def _goal_mult(self, hg, ag):
        if not self.margin_mult:
            return 1.0
        d = abs(hg - ag)
        return math.sqrt(d) if d > 0 else 1.0

    def apply_result(self, home, away, hg, ag):
        """Met à jour les ratings APRÈS le match (score réel)."""
        ra, rb = self.rating(home), self.rating(away)
        eh = self.expected(ra, rb, home=True)
        sh = 1.0 if hg > ag else (0.5 if hg == ag else 0.0)
        mult = self._goal_mult(hg, ag)
        delta = self.k * mult * (sh - eh)
        # zero-sum propre : l'extérieur reçoit l'update complémentaire
        # (son score attendu/réel vaut exactement 1 - celui du domicile).
        self.ratings[home] = ra + delta
        self.ratings[away] = rb - delta
        return ra, rb

    def pre_match(self, home, away):
        """Features Elo STRICTEMENT AVANT le match."""
        ra, rb = self.rating(home), self.rating(away)
        return {
            "elo_home": round(ra, 3),
            "elo_away": round(rb, 3),
            "elo_diff": round(ra + self.home_adv - rb, 3),
            "elo_expect_home": round(self.expected(ra, rb, True), 6),
        }


def build_ratings_through_time(matches, **kw):
    """Itérateur : pour chaque match (ordre chronologique), émet les
    features Elo PRE-match puis applique le résultat.
    `matches` : dicts triés par date avec keys home, away, hg, ag, kickoff_utc."""
    elo = TemporalElo(**kw)
    out = []
    for m in matches:
        pre = elo.pre_match(m["home"], m["away"])
        pre_match_id = m["match_id"]
        out.append((pre_match_id, pre))
        elo.apply_result(m["home"], m["away"], m["hg"], m["ag"])
    return out
