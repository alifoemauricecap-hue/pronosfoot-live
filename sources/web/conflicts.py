# -*- coding: utf-8 -*-
"""
CONFLITS ENTRE SOURCES (2B.WEB-3 §16)
======================================
RÈGLE ABSOLUE : JAMAIS de moyenne silencieuse (A=10, B=12 → 11 interdit).
On CONSERVE les valeurs séparément, puis on applique la politique WEB-0 :

    1. priorité de la source (chaîne registry 2B.1 : fallback_chain)
    2. effective_at le plus récent
    3. published_at / retrieved_at
    4. confiance
    5. fraîcheur
    6. cohérence temporelle

Si le conflit est critique (même rang, valeurs différentes, pas de
départage temporel) → UNKNOWN et le conflit est journalisé.
"""
from sources import registry as _regmod
from .normalized import UNKNOWN, is_unknown

_VALUE_EQ_TOL = 1e-9


def values_equal(a, b):
    if is_unknown(a) or is_unknown(b):
        return is_unknown(a) and is_unknown(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= _VALUE_EQ_TOL
    return a == b


def detect_conflict(points):
    """points = liste de DataPoint (même data_type & cible) — conflit si ≥2
    sources différentes avec valeurs PRÉSENTES différentes."""
    present = [p for p in points if not is_unknown(p.value) and p.valid]
    srcs = {p.source for p in present}
    if len(srcs) < 2:
        return None
    vals = {p.value for p in present
            if not isinstance(p.value, (list, dict))}
    if len(vals) <= 1 and len(present) == len(vals) * (len(present) or 1):
        return None                             # toutes égales → pas de conflit
    seen = []
    for p in present:
        if not any(values_equal(p.value, q.value) for q in seen):
            seen.append(p)
    if len(seen) < 2:
        return None
    return {"data_type": points[0].data_type,
            "values": {p.source: (None if is_unknown(p.value) else p.value)
                       for p in present},
            "conflict": True}


def _source_rank(reg, data_type, source_id):
    """Rang dans la chaîne de priorité 2B.1 (0 = prioritaire ; None = absent)."""
    try:
        chain = _regmod.fallback_chain(reg, data_type)
    except Exception:
        return None
    ids = [s["source_id"] if isinstance(s, dict) else s for s in chain]
    try:
        return ids.index(source_id)
    except ValueError:
        return None


def resolve_conflict(points, data_type, reg=None, journal=None,
                     metrics=None, match_id=None):
    """Retourne (chosen_point_or_None, report). chosen=None ⇒ UNKNOWN.
    Les points restent CONSERVÉS tels quels — on choisit, on ne fusionne pas."""
    reg = reg or _regmod.load()
    present = [p for p in points if not is_unknown(p.value) and p.valid]
    report = {"data_type": data_type, "kept_separate": True,
              "candidates": {}, "decision": None, "winner": None,
              "critical": False}
    for p in present:
        report["candidates"][p.source] = p.value
    if not present:
        report["decision"] = "NO_VALUE"
        return None, report

    def rank_of(p):
        r = _source_rank(reg, data_type, p.source)
        return r if r is not None else 10 ** 6

    # 1) priorité source (rang registry 2B.1)
    ordered = sorted(present, key=rank_of)
    best_rank = rank_of(ordered[0])
    top = [p for p in ordered if rank_of(p) == best_rank]
    chosen = None
    if len(top) == 1:
        chosen = top[0]
        report["decision"] = "PRIORITY_SOURCE"
    else:
        # 2-3) effective_at / retrieved_at les plus récents
        top2 = sorted(top, key=lambda p: (p.effective_at or p.retrieved_at
                                          or ""), reverse=True)
        ts = (top2[0].effective_at or top2[0].retrieved_at or "")[:19]
        tied = [p for p in top2
                if (p.effective_at or p.retrieved_at or "")[:19] == ts]
        if len(tied) == 1:
            chosen = tied[0]
            report["decision"] = "RECENCY_TIEBREAK"
        else:
            tied2 = sorted(tied, key=lambda p: (
                {"high": 3, "medium": 2, "low": 1}.get(p.confidence, 0),
                p.retrieved_at or ""), reverse=True)
            confs = {p.confidence for p in tied2}
            if len(confs) == 1 and len(tied2) > 1:
                # conflit CRITIQUE : pas de départage → UNKNOWN (§16)
                chosen = None
                report["decision"] = "UNKNOWN_CRITICAL_CONFLICT"
                report["critical"] = True
            else:
                chosen = tied2[0]
                report["decision"] = "CONFIDENCE_TIEBREAK"
    report["winner"] = chosen.source if chosen else None
    if journal is not None:
        journal.record(event="CONFLICT", match_id=match_id,
                       data_type=data_type, decision=report["decision"],
                       values=report["candidates"], winner=report["winner"])
    if metrics is not None:
        metrics.record_conflict(
            resolved=(None if report["critical"] else bool(chosen)))
    return chosen, report
