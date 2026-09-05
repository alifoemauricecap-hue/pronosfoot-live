# -*- coding: utf-8 -*-
"""
NORMALIZED DATA (2B.WEB-3 §11/§13/§18/§20)
==========================================
Structures internes COMMUNES à toutes les sources sportives :

    SOURCE → RAW → EXTRACTOR → NORMALIZED → PROVENANCE → VALIDATION

Règles absolues :
- AUCUNE donnée inventée : une valeur absente est UNKNOWN (sentinelle
  explicite), JAMAIS 0, JAMAIS None silencieux, JAMAIS une estimation ;
- UNKNOWN ≠ FALSE ≠ 0 — trois choses différentes, jamais confondues (§R1) ;
- 5 niveaux traçables : RAW, SOURCE_NATIVE, NORMALIZED, DERIVED, AGGREGATED ;
  DERIVED/AGGREGATED portent TOUJOURS derivation_method + model_version +
  inputs — une métrique dérivée n'est JAMAIS présentée comme une stat
  officielle de la source (§8/§18/§20) ;
- chaque DataPoint garde TOUTE sa provenance (§13) et se compare à un
  instant T via la règle 2A provenance.usable_at (anti-leakage §14).

Ce module ne fait AUCUN réseau et N'IMPORTE PAS le socle 2A au module
(db/repository) — la compatibilité 2A se fait par structure de dict
(champs REQUIRED de provenance.py), vérifiable sans import.
"""
from datetime import datetime, timezone

from sources import provenance as _prov

UTC = timezone.utc

RAW = "RAW"
SOURCE_NATIVE = "SOURCE_NATIVE"
NORMALIZED = "NORMALIZED"
DERIVED = "DERIVED"
AGGREGATED = "AGGREGATED"
LEVELS = (RAW, SOURCE_NATIVE, NORMALIZED, DERIVED, AGGREGATED)

MODEL_VERSION = "web3-v1"          # version des méthodes de DÉRIVATION WEB-3


class _Unknown:
    """Sentinelle UNIQUE d'indisponibilité — distincte de 0, None, False."""
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self):
        return "UNKNOWN"

    def __str__(self):
        return "UNKNOWN"

    def __bool__(self):
        raise TypeError("UNKNOWN n'est ni True ni False — expliciter le test")


UNKNOWN = _Unknown()


def is_unknown(v):
    return v is UNKNOWN


def now_iso():
    """Horodatage de MESURE (retrieved_at) — format 2A (ISO ms, Z)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso_ok(s):
    return isinstance(s, str) and _prov.ISO_RE.match(s) is not None


# ---------------------------------------------------------------------------
# DATAPOINT — unité de donnée traçable (§13)
# ---------------------------------------------------------------------------
class DataPoint:
    """Une valeur + TOUTE sa provenance.

    Champs de conformité 2A (provenance.REQUIRED) : value, data_type, source,
    retrieved_at, effective_at, confidence, freshness_sec.
    Extensions WEB-3 : level, source_url, published_at, match_id, team_id,
    player_id, competition_id, valid, issues, derivation_*, inputs.

    to_dict() retourne un dict COMPATIBLE provenance.validate_data_point
    lorsque les champs requis sont remplis (freshness_sec calculé)."""

    __slots__ = ("value", "data_type", "level", "source", "source_url",
                 "retrieved_at", "published_at", "effective_at",
                 "confidence", "match_id", "team_id", "player_id",
                 "competition_id", "valid", "issues",
                 "derivation_method", "model_version", "inputs")

    def __init__(self, value, data_type, source, retrieved_at, *,
                 level=NORMALIZED, source_url=None, published_at=None,
                 effective_at=None, confidence="medium", match_id=None,
                 team_id=None, player_id=None, competition_id=None,
                 valid=True, issues=None, derivation_method=None,
                 model_version=None, inputs=None):
        if level not in LEVELS:
            raise ValueError(f"niveau inconnu : {level!r}")
        if level in (DERIVED, AGGREGATED):
            if not derivation_method or not model_version:
                raise ValueError(
                    f"{level} exige derivation_method + model_version (§8/§18)")
        self.value = value
        self.data_type = data_type
        self.level = level
        self.source = source
        self.source_url = source_url
        self.retrieved_at = retrieved_at
        self.published_at = published_at
        self.effective_at = effective_at
        self.confidence = confidence
        self.match_id = match_id
        self.team_id = team_id
        self.player_id = player_id
        self.competition_id = competition_id
        self.valid = bool(valid)
        self.issues = list(issues or [])
        self.derivation_method = derivation_method
        self.model_version = model_version
        self.inputs = list(inputs or [])

    # -- provenance 2A compat ------------------------------------------------
    def to_dict(self, as_of=None):
        d = {
            "value": ("UNKNOWN" if is_unknown(self.value) else self.value),
            "data_type": self.data_type,
            "level": self.level,
            "source": self.source,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "published_at": self.published_at,
            "effective_at": self.effective_at,
            "confidence": self.confidence,
            "freshness_sec": self.freshness_sec(as_of),
            "match_id": self.match_id,
            "team_id": self.team_id,
            "player_id": self.player_id,
            "competition_id": self.competition_id,
            "valid": self.valid,
            "issues": list(self.issues),
        }
        if self.level in (DERIVED, AGGREGATED):
            d.update({"derivation_method": self.derivation_method,
                      "model_version": self.model_version,
                      "inputs": list(self.inputs)})
        return d

    # échelle textuelle WEB-3 → projection numérique attendue par 2A (§compat)
    _CONF_2A = {"high": 0.9, "medium": 0.6, "low": 0.3, "unknown": 0.0}

    def as_prov_dict(self, as_of=None):
        """PROJECTION strictement 2A (champs REQUIRED) — validable par
        provenance.validate_data_point. Projection documentée : la confiance
        textuelle WEB-3 devient numérique via _CONF_2A ; freshness_sec=None
        devient 0 ; effective_at=None devient retrieved_at. Les valeurs
        ORIGINALES restent inchangées dans ce DataPoint (rien n'est perdu)."""
        conf = self.confidence
        cnum = conf if isinstance(conf, (int, float)) and not \
            isinstance(conf, bool) else self._CONF_2A.get(conf, 0.0)
        fresh = self.freshness_sec(as_of)
        return {
            "value": self.value,
            "data_type": self.data_type,
            "source": self.source,
            "retrieved_at": self.retrieved_at,
            "effective_at": self.effective_at or self.retrieved_at,
            "confidence": cnum,
            "freshness_sec": fresh if isinstance(fresh, (int, float)) else 0,
        }

    def validate_2a(self):
        """True si compatible avec provenance.validate_data_point (2A)."""
        try:
            _prov.validate_data_point(self.as_prov_dict())
            return True
        except Exception:
            return False

    # -- temps ---------------------------------------------------------------
    def freshness_sec(self, as_of=None):
        """Âge (secondes) de la donnée au moment as_of (défaut : maintenant)."""
        ref = as_of or now_iso()
        try:
            return max(0, _sec(ref) - _sec(self.retrieved_at))
        except Exception:
            return None

    def usable_at(self, T):
        """ANTI-LEAKAGE : utilisable pour une prédiction datée T ?
        Règle : effective_at <= T ET retrieved_at <= T (§14).
        retrieved_at / effective_at au format ISO (comparaison [:19] 2A)."""
        if not (isinstance(T, str) and _iso_ok(T)):
            return False
        r = self.retrieved_at
        if not (isinstance(r, str) and r[:19] <= T[:19]):
            return False
        e = self.effective_at
        if e is None:
            return True                        # sans date d'effet → mesure T
        return isinstance(e, str) and e[:19] <= T[:19]

    def clone_invalid(self, issue):
        self.valid = False
        self.issues.append(issue)
        return self

    def __repr__(self):
        return (f"DataPoint({self.data_type}={self.value!r} src={self.source} "
                f"lvl={self.level} valid={self.valid})")


def _sec(iso):
    """Secondes epoch approx d'un ISO…Z (comparaisons uniquement)."""
    dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    return int(dt.timestamp())


def new_datapoint(value, data_type, source, retrieved_at=None, **kw):
    """Factory : value=None/UNKNOWN → UNKNOWN explicite (jamais 0)."""
    if value is None or is_unknown(value):
        value = UNKNOWN
        kw.setdefault("confidence", "unknown")
    return DataPoint(value, data_type, source,
                     retrieved_at or now_iso(), **kw)


def unknown_point(data_type, source, retrieved_at=None, **kw):
    """Raccourci explicite : la donnée N'EXISTE PAS à la source (§R1)."""
    kw.setdefault("confidence", "unknown")
    kw.setdefault("issues", ["NOT_AVAILABLE_AT_SOURCE"])
    return DataPoint(UNKNOWN, data_type, source,
                     retrieved_at or now_iso(), **kw)
