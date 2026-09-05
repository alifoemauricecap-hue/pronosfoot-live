# -*- coding: utf-8 -*-
"""
CANDIDATS 2C (§8/§15)
======================
MODEL_2C_A — baseline statistique (Poisson, moyennes de ligue du PASSÉ seul)
MODEL_2C_B — Elo (prior petits échantillons) + attaque/défense shrinkagée
             + domicile → Poisson   [cohérent, anti double comptage]
MODEL_2C_C — Dixon-Coles ML pondéré temps (+ xG RÉEL si disponible : C-xG)
MODEL_2C_D — ensemble pondéré B/C retenu SEULEMENT s'il bat B et C sur
             pli de validation disjoint (sinon NOT_JUSTIFIED)

Règles : aucun modèle ne choisit ses hyperparamètres sur le test ;
chaque prédiction contextuelle porte data_level + data_quality ;
INSUFFICIENT_DATA → NO_PREDICTION (pas de fausse précision).
"""

from .poisson2c import score_matrix, markets_from_matrix, clamp
from .config2c import CONFIG_2C as C
from .guards import data_level, prediction_allowed, NO_PREDICTION, DataQuality

BASE_VERSION = "2c-model-1.0"


def _lg_lambdas(strength_feats):
    lg_h = strength_feats.get("league_home_avg") or 1.45
    lg_a = strength_feats.get("league_away_avg") or 1.15
    return lg_h, lg_a


class Model2C_A:
    """Baseline : lambdas = moyennes de ligue traînantes (passé seul)."""
    name = "MODEL_2C_A"
    version = BASE_VERSION

    def predict(self, feats):
        lg_h, lg_a = _lg_lambdas(feats)
        m, lh, la = score_matrix(lg_h, lg_a, C["max_goals_grid"])
        return {"matrix": m, "lambda_h": lh, "lambda_a": la,
                "markets": markets_from_matrix(m), "model": self.name,
                "version": self.version}


class Model2C_B:
    """Elo (prior faible échantillon) + att/def shrinkagée + domicile → Poisson."""
    name = "MODEL_2C_B"
    version = BASE_VERSION

    def predict(self, feats):
        lg_h, lg_a = _lg_lambdas(feats)
        att_h, def_h = feats.get("att_home", 1.0), feats.get("def_home", 1.0)
        att_a, def_a = feats.get("att_away", 1.0), feats.get("def_away", 1.0)
        lam_h = lg_h * att_h * def_a
        lam_a = lg_a * att_a * def_h
        m, lh, la = score_matrix(lam_h, lam_a, C["max_goals_grid"])
        return {"matrix": m, "lambda_h": lh, "lambda_a": la,
                "markets": markets_from_matrix(m), "model": self.name,
                "version": self.version}


class Model2C_C:
    """Dixon-Coles (params ML ajustés sur le train seul, refit par pli)."""
    name = "MODEL_2C_C"
    version = BASE_VERSION

    def __init__(self, dc_params):
        self.params = dc_params  # None si fit impossible (honnêteté)

    @property
    def available(self):
        return self.params is not None

    def predict(self, home, away):
        if not self.available:
            return None
        m, lh, la = self.params.predict(home, away)
        return {"matrix": m, "lambda_h": lh, "lambda_a": la,
                "markets": markets_from_matrix(m), "model": self.name,
                "version": self.version,
                "xg_used": self.params.xg_used, "n_train": self.params.n_train}


class Model2C_D:
    """Ensemble B/C : moyenne pondérée des matrices de score.
    `weight_c` choisi sur pli de validation disjoint (jamais sur le test)."""
    name = "MODEL_2C_D"
    version = BASE_VERSION

    def __init__(self, weight_c):
        self.weight_c = clamp(weight_c, 0.0, 1.0)

    def predict(self, pred_b, pred_c, home, away):
        if pred_c is None:
            return None  # sans C, pas d'ensemble — honnêteté
        w = self.weight_c
        mb, mc = pred_b["matrix"], pred_c["matrix"]
        n = len(mb)
        mix = [[w * mc[i][j] + (1 - w) * mb[i][j] for j in range(n)] for i in range(n)]
        tot = sum(sum(r) for r in mix)
        mix = [[v / tot for v in r] for r in mix]
        return {"matrix": mix, "markets": markets_from_matrix(mix),
                "model": self.name, "version": self.version,
                "weight_c": w, "xg_used": pred_c.get("xg_used", False)}


def predict_all(feats, home, away, dc_params, weight_c=None):
    """Prédiction contextuelle complète : niveau de données + NO_PREDICTION
    honnête + distribution de chaque modèle disponible."""
    lvl = data_level(feats.get("home_matches") or feats.get("home_form_n_w15") or 0,
                     feats.get("away_matches") or feats.get("away_form_n_w15") or 0)
    if not prediction_allowed(lvl):
        return {"status": NO_PREDICTION, "data_level": lvl,
                "reason": "INSUFFICIENT_DATA — mieux vaut aucune prédiction qu'une fausse précision"}
    a = Model2C_A().predict(feats)
    b = Model2C_B().predict(feats)
    c = Model2C_C(dc_params).predict(home, away)
    d = Model2C_D(weight_c).predict(b, c, home, away) if weight_c is not None and c else None
    dq = DataQuality(lvl, feats.get("home_matches") or 0, feats.get("away_matches") or 0,
                     bool((c or {}).get("xg_used")), feats.get("_sources") or ["openligadb"])
    out = {"status": "OK", "data_level": lvl, "data_quality": dq.as_dict(),
           "models": {"A": a, "B": b, "C": c, "D": d}}
    return out
