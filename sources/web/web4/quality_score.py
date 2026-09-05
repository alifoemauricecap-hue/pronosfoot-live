# -*- coding: utf-8 -*-
"""
SCORE DE QUALITÉ DE LA DONNÉE (2B.WEB-4 §20 — formule WEB-0)
=============================================================

    score = 0.30 · reliability
          + 0.25 · freshness
          + 0.20 · cross_source_confirmation
          + 0.15 · temporal_validity
          + 0.10 · completeness

⚠️  CE SCORE EST UN SCORE DE QUALITÉ DE DONNÉE.
    Ce N'EST PAS une probabilité de victoire. Il est INTERDIT de l'afficher
    comme « probabilité 87 % » ou de l'injecter dans le modèle (§20/§39).
    Il sert à l'observabilité et aux candidats de snapshot (meta).
"""
from ..normalized import is_unknown
from .. import freshness as _fresh

from .config import WEB4_CONFIG


def data_quality_score(points, registry, as_of=None, expected_types=None,
                       weights=None):
    """Retourne un dict {components, score (0–1), grade, label}.

    points : DataPoint du match (valides + UNKNOWN — les UNKNOWN dégradent
             honnêtement freshness/completeness) ;
    expected_types : data_types attendus pour ce contexte (défaut : ceux
             observés — completeness=1 si rien n'est déclaré attendu).
    """
    w = weights or WEB4_CONFIG["quality_weights"]
    pts = list(points or [])
    present = [p for p in pts if not is_unknown(p.value)]

    # -- reliability : moyenne des fiabilités registry des sources présentes
    srcs = (registry or {}).get("sources") or {}
    if isinstance(srcs, list):              # registry réel : liste de sources
        srcs = {s.get("source_id"): s for s in srcs if isinstance(s, dict)}
    rels = []
    for p in present:
        src = srcs.get(p.source) or {}
        r = src.get("reliability")
        if isinstance(r, dict):              # registry réel : {score, basis}
            r = r.get("score")
        try:
            rels.append(float(r) if r is not None else 0.0)
        except (TypeError, ValueError):
            rels.append(0.0)
    reliability = sum(rels) / len(rels) if rels else 0.0

    # -- freshness : ratio âge/TTL par point (TTL registry = source de vérité)
    fr = []
    if as_of is not None:
        for p in pts:
            if is_unknown(p.value):
                fr.append(0.0)
                continue
            try:
                status, age, ttl = _fresh.freshness_status(
                    p, as_of, p.source, registry)
            except Exception:
                fr.append(0.0)
                continue
            if status in (_fresh.NO_EXPIRY,):
                fr.append(1.0)
            elif ttl and age is not None:
                fr.append(max(0.0, 1.0 - (age / float(ttl))))
            else:
                fr.append(0.0)
    freshness_c = sum(fr) / len(fr) if fr else 0.0

    # -- confirmation croisée : part des types confirmés par ≥2 sources
    by_type = {}
    for p in present:
        by_type.setdefault(p.data_type, set()).add(p.source)
    seen_types = {p.data_type for p in pts}
    if seen_types:
        confirmed = sum(1 for dt in seen_types
                        if len(by_type.get(dt, ())) >= 2)
        confirmation = confirmed / len(seen_types)
    else:
        confirmation = 0.0

    # -- temporal_validity : part utilisable à as_of (§14)
    if as_of is not None and pts:
        temporal = sum(1 for p in pts if p.usable_at(as_of)) / len(pts)
    else:
        temporal = 1.0 if pts else 0.0

    # -- completeness : types présents (non-UNKNOWN) / types attendus
    expected = set(expected_types or seen_types)
    if expected:
        completeness = len({p.data_type for p in present} & expected) / len(expected)
    else:
        completeness = 0.0

    score = (w["reliability"] * reliability + w["freshness"] * freshness_c +
             w["confirmation"] * confirmation +
             w["temporal_validity"] * temporal +
             w["completeness"] * completeness)
    score = max(0.0, min(1.0, round(score, 4)))
    grade = ("A" if score >= 0.85 else "B" if score >= 0.70 else
             "C" if score >= 0.50 else "D")
    return {
        "label": "DATA_QUALITY_SCORE — qualité de la donnée, PAS une "
                 "probabilité de victoire (§20)",
        "weights": dict(w),
        "components": {
            "reliability": round(reliability, 4),
            "freshness": round(freshness_c, 4),
            "confirmation": round(confirmation, 4),
            "temporal_validity": round(temporal, 4),
            "completeness": round(completeness, 4),
        },
        "score": score,
        "grade": grade,
        "points_total": len(pts),
        "points_present": len(present),
    }
