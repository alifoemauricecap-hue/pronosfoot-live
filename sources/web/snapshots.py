# -*- coding: utf-8 -*-
"""
SNAPSHOT BUILDER 2A — intégration (2B.WEB-3 §24)
=================================================
La SEULE voie par laquelle les données WEB-3 deviennent persistantes :
`repository.insert_snapshot` (2A) — INSERT-ONLY, hashé, jamais modifié.

Règles :
- le payload ne contient QUE les points utilisables à as_of (anti-leakage
  §14 : retrieved_at<=as_of ET effective_at<=as_of) ;
- après gel d'un snapshot : INTERDICTION de le modifier — une nouvelle
  information crée un NOUVEAU snapshot (ligne supplémentaire), jamais une
  réécriture (garanti par insert_snapshot lui-même) ;
- ce module n'est PAS branché sur le live existant (prediction_service
  continue d'utiliser son propre chemin inchangé) — c'est la voie de
  l'ingestion WEB-3, activable plus tard (WEB-4) avec validation.
"""
from .normalized import UNKNOWN, now_iso


def build_snapshot_payload(report, as_of):
    """ResearchReport.to_dict() filtré anti-leakage à as_of.
    Retourne (payload, available_fields, quality)."""
    data = {}
    for dp in report.points:
        if not dp.usable_at(as_of):            # §14 — point futur exclu
            continue
        data.setdefault(dp.data_type, []).append(dp.to_dict(as_of))
    # available_fields = types ayant AU MOINS une valeur présente et valide
    present = [dt for dt, points in data.items()
               if any(p["value"] != "UNKNOWN" and p["valid"] for p in points)]
    unknown_fields = [dt for dt in data if dt not in present]
    quality = _quality(len(present), len(unknown_fields), len(report.errors))
    return ({"as_of": as_of,
             "match_id": report.match_id,
             "generated_by": "web3-pipeline",
             "data": data,
             "meta": {"requests_used": report.requests_used,
                      "fallbacks": report.fallbacks,
                      "errors": report.errors,
                      "conflicts": report.conflicts,
                      "identity": report.identity,
                      "snapshot_policy": "insert-only, anti-leakage §14"}},
            sorted(set(present)), quality)


def _quality(n_present, n_unknown, n_errors):
    if n_present >= 8 and n_errors == 0:
        return "high"
    if n_present >= 3:
        return "medium"
    return "low"


def persist_snapshot(report, as_of, match_id, source="web3",
                     repository=None):
    """Persiste via repository.insert_snapshot (2A). repository injectable
    pour les tests (ou importé paresseusement — le seul pont vers le socle,
    explicitement déclaré §24)."""
    if repository is None:
        import repository as repository        # pont 2A DÉCLARÉ (§24)
    payload, fields, quality = build_snapshot_payload(report, as_of)
    snap = repository.insert_snapshot(match_id, as_of, source, payload,
                                      quality, fields)
    return {"snapshot_id": snap["id"], "hash": snap["hash"],
            "available_fields": fields, "data_quality": quality,
            "payload": payload}
