# -*- coding: utf-8 -*-
"""
CALIBRATION (§17)
==================
Implémentations internes (pas de sklearn — dépendance évitée, code audité) :
- Platt scaling : régression logistique sur le logit de la proba brute
  (descente de gradient, hyperparamètres dans config) ;
- isotone : PAVA (pool adjacent violators) sur fréquences par paires,
  prédiction par paliers interpolés.

RÈGLES ABSOLUES :
- la calibration est entraînée sur une PÉRIODE DISTINCTE du test final
  (le pipeline garantit la disjointure temporelle) ;
- raw_probability ET calibrated_probability sont toujours conservées ;
- jamais de calibration apprise sur le test final.
"""

import math
from .guards import clamp_prob
from .config2c import CONFIG_2C as C


def _logit(p):
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


def _sigmoid(z):
    return 1.0 / (1.0 + math.exp(-z))


class PlattCalibrator:
    """p_cal = sigmoid(a * logit(p_raw) + b). Fit = GD sur log-vraisemblance."""

    def __init__(self):
        self.a, self.b, self.n = 1.0, 0.0, 0
        self.trained = False

    def fit(self, raw, outcomes):
        pairs = [(clamp_prob(p), 1.0 if o else 0.0) for p, o in zip(raw, outcomes)]
        self.n = len(pairs)
        if self.n < C["isotonic_min_pairs"]:
            self.trained = False
            return self
        a, b, lr = 1.0, 0.0, C["platt_lr"]
        for _ in range(C["platt_iters"]):
            ga = gb = 0.0
            for p, o in pairs:
                z = _logit(p)
                q = _sigmoid(a * z + b)
                ga += (q - o) * z
                gb += (q - o)
            ga /= self.n
            gb /= self.n
            a -= lr * ga
            b -= lr * gb
            a = min(max(a, -8.0), 8.0)
            b = min(max(b, -8.0), 8.0)
        self.a, self.b = a, b
        self.trained = True
        return self

    def predict(self, p):
        if not self.trained:
            return clamp_prob(p)
        return clamp_prob(_sigmoid(self.a * _logit(p) + self.b))


class IsotonicCalibrator:
    """PAVA : fréquences observées non-décroissantes en fonction de p brut."""

    def __init__(self):
        self.xs, self.ys, self.n = [], [], 0
        self.trained = False

    def fit(self, raw, outcomes):
        pts = sorted(zip(raw, outcomes), key=lambda t: t[0])
        self.n = len(pts)
        if self.n < C["isotonic_min_pairs"]:
            self.trained = False
            return self
        # blocs (poids, somme_y, somme_p)
        blocks = [[1.0, float(o), float(p)] for p, o in pts]
        i = 0
        while i < len(blocks) - 1:
            mu1 = blocks[i][1] / blocks[i][0]
            mu2 = blocks[i + 1][1] / blocks[i + 1][0]
            if mu1 > mu2:  # violation → fusion (pool adjacent violators)
                blocks[i:i + 2] = [[blocks[i][0] + blocks[i + 1][0],
                                    blocks[i][1] + blocks[i + 1][1],
                                    blocks[i][2] + blocks[i + 1][2]]]
                i = max(0, i - 1)
            else:
                i += 1
        self.xs = [b[2] / b[0] for b in blocks]
        self.ys = [min(max(b[1] / b[0], 0.0), 1.0) for b in blocks]
        self.trained = True
        return self

    def predict(self, p):
        if not self.trained:
            return clamp_prob(p)
        xs, ys = self.xs, self.ys
        if p <= xs[0]:
            return clamp_prob(ys[0])
        if p >= xs[-1]:
            return clamp_prob(ys[-1])
        import bisect
        i = bisect.bisect_right(xs, p) - 1
        return clamp_prob(ys[i])


class MulticlassCalibrator:
    """Calibre chaque classe one-vs-rest puis RENORMALISE (somme = 1)."""

    def __init__(self, method="platt"):
        assert method in ("platt", "isotonic")
        self.method = method
        self.per_class = {}
        self.trained_classes = 0

    def _mk(self):
        return PlattCalibrator() if self.method == "platt" else IsotonicCalibrator()

    def fit(self, raw_dists, outcomes, classes=("1", "N", "2")):
        for c in classes:
            cal = self._mk().fit([d[c] for d in raw_dists],
                                 [1 if o == c else 0 for o in outcomes])
            self.per_class[c] = cal
            self.trained_classes += 1 if cal.trained else 0
        return self

    def predict(self, dist):
        vals = {}
        for c, p in dist.items():
            cal = self.per_class.get(c)
            vals[c] = cal.predict(p) if cal else clamp_prob(p)
        tot = sum(vals.values())
        if tot <= 0:
            return {c: 1.0 / len(vals) for c in vals}
        return {c: clamp_prob(v / tot) for c, v in vals.items()}

    @property
    def trained(self):
        return self.trained_classes > 0
