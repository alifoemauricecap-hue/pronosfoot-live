# -*- coding: utf-8 -*-
"""
FORCES ATTAQUE / DÉFENSE (§7) — petits échantillons respectés
==============================================================
Estimation EN LIGNE (strictement matchs passés) :
- pondération par récence (demi-vie configurable) calculée AU MOMENT DE LA
  REQUÊTE T — jamais à l'update (le test test_many_matches_less_shrinkage a
  prouvé que pondérer à l'update casse la récence : bug corrigé, D2C-bug-1) ;
- SHRINKAGE vers la moyenne de ligue (prior) : une équipe avec peu de
  données obtient une estimation PRUDENTE, jamais une fausse précision ;
- split domicile/extérieur pondéré par l'échantillon disponible ;
- prior Elo UNIQUEMENT tant que l'équipe manque de matchs
  (anti double comptage D2C-6) ;
- xG RÉEL en option : moyenne pondérée buts/xG quand il existe,
  sinon UNKNOWN et les ratios xG restent à None.

Ratios retournés : att_home, att_away, def_home, def_away (× moyenne ligue).
"""

from .availability import parse_ts
from .config2c import CONFIG_2C as C


def _w(age_days, half_life):
    return 0.5 ** (max(0.0, age_days) / half_life)


class OnlineStrengths:
    """Historiques bruts par équipe + agrégation pondérée à la requête.
    Usage : pour chaque match chronologique → features_pre(T) PUIS update().
    """

    def __init__(self, half_life_days=None, shrink_k=None, home_weight=None,
                 source="openligadb", decay_window_days=370):
        self.hl = half_life_days or C["strength_half_life_days"]
        self.k = shrink_k or C["strength_shrink_k"]
        self.hw = home_weight or C["strength_home_weight"]
        self.window = decay_window_days
        self.source = source
        self.teams = {}     # team -> list of (day, gf, ga, loc)
        self.league = []    # (day, hg, ag)
        self.xg = {}        # team -> list of (day, xg_for, xg_against)

    # ---- internes ----
    def _team(self, name):
        return self.teams.setdefault(name, [])

    def _weighted_rates(self, events, as_of):
        """(n, sw, gf/sw, ga/sw, home trio, away trio) à l'instant T,
        sur la fenêtre traînante [T-window, T)."""
        t = parse_ts(as_of)
        n = 0
        sw = gf = ga = 0.0
        h_n = a_n = 0
        h_sw = h_gf = h_ga = 0.0
        a_sw = a_gf = a_ga = 0.0
        for day, f, g, loc in events:
            age = (t - day).total_seconds() / 86400.0
            if age <= 0 or age > self.window:
                continue
            w = _w(age, self.hl)
            n += 1
            sw += w
            gf += w * f
            ga += w * g
            if loc == "home":
                h_n += 1
                h_sw += w
                h_gf += w * f
                h_ga += w * g
            else:
                a_n += 1
                a_sw += w
                a_gf += w * f
                a_ga += w * g
        return {"n": n, "sw": sw, "gf": gf, "ga": ga,
                "h_n": h_n, "h_sw": h_sw, "h_gf": h_gf, "h_ga": h_ga,
                "a_n": a_n, "a_sw": a_sw, "a_gf": a_gf, "a_ga": a_ga}

    def league_avgs(self, as_of):
        t = parse_ts(as_of)
        hw = hgf = aw = agf = 0.0
        n = 0
        for day, hg, ag in self.league:
            age = (t - day).total_seconds() / 86400.0
            if age <= 0 or age > self.window:
                continue
            w = _w(age, self.hl)
            hw += w
            hgf += w * hg
            aw += w
            agf += w * ag
            n += 1
        if n < 5:
            return 1.45, 1.15  # amorçage ligue (documenté) tant que données maigres
        return hgf / hw, agf / aw

    def _shrink(self, rate, w, prior):
        return (rate * w + prior * self.k) / (w + self.k)

    # ---- features avant match ----
    def features_pre(self, home, away, as_of, elo_home=None, elo_away=None,
                     xg_available=False):
        """Ratios d'attaque/défense STRICTEMENT AVANT le match `as_of`."""
        lg_h, lg_a = self.league_avgs(as_of)
        out = {"league_home_avg": round(lg_h, 5), "league_away_avg": round(lg_a, 5)}

        for side, team, elo in (("home", home, elo_home), ("away", away, elo_away)):
            ev = self.teams.get(team) or []
            r = self._weighted_rates(ev, as_of)
            if r["n"] == 0 or r["sw"] < 1e-9:
                ratio = 1.0
                if elo is not None:
                    ratio = max(0.6, min(1.6, (elo / C["elo_base"]) ** 0.5))
                out[f"{side}_matches"] = 0
                out[f"att_{side}"] = round(ratio, 6)
                out[f"def_{side}"] = round(ratio, 6)
                out[f"{side}_shrinkage_applied"] = True
            else:
                rate_for = r["gf"] / r["sw"]
                rate_against = r["ga"] / r["sw"]
                att = self._shrink(rate_for, r["sw"], lg_h) / lg_h
                dfn = self._shrink(rate_against, r["sw"], lg_a) / lg_a

                loc_key = "h" if side == "home" else "a"
                loc_n = r[f"{loc_key}_n"]
                loc_sw = r[f"{loc_key}_sw"]
                if loc_n >= C["strength_min_loc_matches"] and loc_sw > C["strength_min_loc_weight"]:
                    loc_avg = lg_h if side == "home" else lg_a
                    att_loc = self._shrink(r[f"{loc_key}_gf"] / loc_sw, loc_sw, loc_avg) / loc_avg
                    def_loc = self._shrink(r[f"{loc_key}_ga"] / loc_sw, loc_sw, loc_avg) / loc_avg
                    att = self.hw * att_loc + (1 - self.hw) * att
                    dfn = self.hw * def_loc + (1 - self.hw) * dfn

                if elo is not None and r["n"] < C["elo_prior_matches_full"]:
                    w_elo = 1.0 - r["n"] / C["elo_prior_matches_full"]
                    elo_ratio = max(0.6, min(1.6, (elo / C["elo_base"]) ** 0.5))
                    att = (1 - w_elo) * att + w_elo * elo_ratio
                    dfn = (1 - w_elo) * dfn + w_elo * elo_ratio

                out[f"{side}_matches"] = r["n"]
                out[f"att_{side}"] = round(max(0.2, min(4.0, att)), 6)
                out[f"def_{side}"] = round(max(0.2, min(4.0, dfn)), 6)
                out[f"{side}_shrinkage_applied"] = r["n"] < C["full_min_matches"]

            xg_rates = self._xg_rates(team, as_of, lg_h, lg_a) if xg_available else None
            if xg_rates:
                out[f"xg_att_{side}"] = xg_rates["xg_att"]
                out[f"xg_def_{side}"] = xg_rates["xg_def"]
                out[f"xg_matches_{side}"] = xg_rates["xg_n"]
            else:
                out[f"xg_att_{side}"] = None
                out[f"xg_def_{side}"] = None
                out[f"xg_matches_{side}"] = 0
        return out

    def _xg_rates(self, team, as_of, lg_h, lg_a):
        """Ratios xG RÉELS (moyenne par match, simple — clarté > astuce)."""
        evs = self.xg.get(team) or []
        t = parse_ts(as_of)
        sel = [e for e in evs if 0 < (t - e[0]).total_seconds() / 86400.0 <= self.window]
        if len(sel) < 3:
            return None
        xf = sum(e[1] for e in sel) / len(sel)
        xa = sum(e[2] for e in sel) / len(sel)
        return {"xg_att": round(xf / lg_h, 6) if lg_h else None,
                "xg_def": round(xa / lg_a, 6) if lg_a else None,
                "xg_n": len(sel)}

    # ---- mise à jour après match ----
    def update(self, home, away, hg, ag, kickoff_utc, as_of,
               xg_home=None, xg_away=None):
        """Ajoute le match TERMINÉ (score réel) aux historiques BRUTS."""
        ko = parse_ts(kickoff_utc)
        self._team(home).append((ko, hg, ag, "home"))
        self._team(away).append((ko, ag, hg, "away"))
        self.league.append((ko, hg, ag))
        # xG RÉEL (StatsBomb) — jamais de xG inventé
        if xg_home is not None:
            self.xg.setdefault(home, []).append((ko, float(xg_home), float(xg_away)))
        if xg_away is not None:
            self.xg.setdefault(away, []).append((ko, float(xg_away), float(xg_home)))
