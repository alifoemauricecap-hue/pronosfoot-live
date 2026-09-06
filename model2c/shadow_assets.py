# -*- coding: utf-8 -*-
"""
ÉTAPE 2C.1 — CHARGEUR DES ASSETS FIGÉS (prod)
==============================================
Charge le seed historique RÉEL (OpenLigaDB 2016→2025) et la calibration
Platt FIGÉE (entraînée offline sur le passé strict — jamais sur la prod, §10).

Toute incohérence de hash est détectée par le manifeste (§17 : alerte
« hash incohérent » côté runner).
"""

import hashlib
import json
import os

from .calibration import PlattCalibrator

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prod_assets")
_CACHE = {}


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _load_json(name):
    with open(os.path.join(_DIR, name), "r", encoding="utf-8") as f:
        return json.load(f)


def available():
    """True si les 3 assets existent (sinon le shadow se refuse — alerte)."""
    return all(os.path.exists(os.path.join(_DIR, n))
               for n in ("seed_bl_history.json", "calibration_2c_v1.json",
                         "manifest.json"))


def manifest():
    if "manifest" not in _CACHE:
        _CACHE["manifest"] = _load_json("manifest.json")
    return dict(_CACHE["manifest"])


def verify_hashes():
    """Vérifie seed/calibration contre le manifeste. (ok, détail)"""
    man = manifest()
    problems = []
    for name, key in (("seed_bl_history.json", "seed_sha256"),
                      ("calibration_2c_v1.json", "calibration_sha256")):
        actual = _sha_file(os.path.join(_DIR, name))
        if man.get(key) and actual != man[key]:
            problems.append({"asset": name, "expected": man[key], "actual": actual})
    return (not problems), problems


def dataset_hash():
    return manifest().get("dataset_hash", "")


def frozen_hyperparams():
    return dict(manifest().get("frozen_hyperparams")
                or {"elo_k": 32, "dc_xi": 0.003, "weight_c": 0.5})


def seed_matches():
    """Matchs du seed au format canonique pipeline (triés chrono)."""
    if "seed" not in _CACHE:
        raw = _load_json("seed_bl_history.json")["matches"]
        out = [{"match_id": f"seed:{lg}:{i}", "league": lg, "season": int(seas),
                "kickoff_utc": ko, "home": h, "away": a,
                "hg": int(hg), "ag": int(ag),
                "effective_at": ko, "source": "seed:openligadb"}
               for i, (lg, seas, ko, h, a, hg, ag) in enumerate(raw)]
        out.sort(key=lambda m: m["kickoff_utc"])
        _CACHE["seed"] = out
    return list(_CACHE["seed"])


def seed_teams():
    return {m["home"] for m in seed_matches()} | {m["away"] for m in seed_matches()}


class FrozenMulticlassPlatt:
    """Ré-applique une calibration FIGÉE (a,b par classe) — aucun fit ici.
    Même sémantique que MulticlassCalibrator('platt').predict + renormalise."""

    def __init__(self, model_payload):
        self.n = int(model_payload.get("n") or 0)
        self.per_class = {}
        for c, p in (model_payload.get("classes") or {}).items():
            cal = PlattCalibrator()
            cal.a, cal.b = float(p["a"]), float(p["b"])
            cal.n = int(p.get("n") or 0)
            cal.trained = bool(p.get("trained"))
            self.per_class[c] = cal

    @property
    def trained(self):
        return any(c.trained for c in self.per_class.values())

    def predict(self, dist):
        vals = {}
        for c, p in dist.items():
            cal = self.per_class.get(c)
            vals[c] = cal.predict(p) if cal else p
        tot = sum(vals.values())
        if tot <= 0:
            return {c: 1.0 / len(vals) for c in vals}
        return {c: vals[c] / tot for c in vals}


def load_calibration():
    """{model_id: FrozenMulticlassPlatt} + métadonnées. None si manquant."""
    if "calib" not in _CACHE:
        if not available():
            _CACHE["calib"] = None
        else:
            payload = _load_json("calibration_2c_v1.json")
            _CACHE["calib"] = {
                "calibration_version": payload.get("calibration_version"),
                "method": payload.get("method"),
                "trained_on": payload.get("trained_on"),
                "models": {mid: FrozenMulticlassPlatt(mp)
                           for mid, mp in (payload.get("models") or {}).items()}}
    return _CACHE["calib"]
