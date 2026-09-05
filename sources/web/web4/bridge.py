# -*- coding: utf-8 -*-
"""
BRIDGE WEB-4 → SNAPSHOT 2A (2B.WEB-4 §12 à §16)
================================================
LE pont propre :

    WEB DATA → POINT-IN-TIME STORE → SNAPSHOT BUILDER 2A

avec `as_of` exact : seuls les points usable_at(as_of) entrent dans le
payload (§12 anti-leakage).

RÈGLES DURES :
- le bridge crée des snapshots CANDIDATS (source="web4-candidate") via
  repository.insert_snapshot — INSERT-ONLY, hashé, JAMAIS modifié après
  coup (§13) ;
- les candidats NE SONT JAMAIS référencés par la table `predictions` :
  le branchement prédictions nécessite une validation explicite (canary
  §35) — le moteur historique reste protégé ;
- KICKOFF PROTECTION (§15) : now >= kickoff ⇒ REFUS + alerte CRITICAL +
  journal ; après kickoff, AUCUN candidat pré-match ;
- MATCHS TERMINÉS (§16) : jamais de reconstruction — settlement uniquement
  (chemin 2A existant, non touché) ;
- §14 : T-15 reste la référence d'évaluation 2A ; T-180/T-60 ne remplacent
  rien — les versions 2A v1/v2/v3 sont conservées par prediction_service.
"""
import json

import db as _db

from ..normalized import now_iso, is_unknown
from . import metrics as _metrics
from .config import WEB4_CONFIG
from .quality_score import data_quality_score

CANDIDATE_SOURCE = WEB4_CONFIG["candidate_source"]
WINDOWS = WEB4_CONFIG["snapshot_windows"]


def snapshot_hash_exists(match_id, payload_hash, source=CANDIDATE_SOURCE,
                         db_module=None):
    """Ce candidat EXACT (même hash) existe-t-il déjà ? (anti-spam de
    lignes identiques — pas de « versions » sans changement de données,
    esprit §8 2A)."""
    dbm = db_module or _db
    r = dbm.query(
        """SELECT 1 AS x FROM data_snapshots
           WHERE match_id=%s AND source=%s AND payload_hash=%s LIMIT 1""",
        (match_id, source, payload_hash), one=True)
    return r is not None


def build_candidate_payload(points, match_id, as_of, t_window, meta=None,
                            registry=None):
    """Payload du candidat : UNIQUEMENT les points usable_at(as_of) (§12).
    Structure alignée sur le builder WEB-3 + fenêtre T + score de QUALITÉ
    DE DONNÉE (§20 — jamais une probabilité de victoire)."""
    usable = [p for p in points if p.usable_at(as_of)]
    data = {}
    for dp in usable:
        data.setdefault(dp.data_type, []).append(dp.to_dict(as_of))
    present = sorted({dt for dt, pts in data.items()
                      if any(p["value"] != "UNKNOWN" and p["valid"]
                             for p in pts)})
    unknown_fields = sorted(dt for dt in data if dt not in present)
    qscore = data_quality_score(usable, registry or {"sources": {}},
                                as_of=as_of)
    return ({
        "as_of": as_of,
        "match_id": match_id,
        "t_window": t_window,
        "generated_by": "web4-bridge",
        "candidate": True,
        "data": data,
        "meta": {
            "policy": "insert-only, anti-leakage §12/§14, candidate §35",
            "usable_points": len(usable),
            "unknown_fields": unknown_fields,
            "data_quality_score": qscore,
            **(meta or {}),
        },
    }, present, qscore)


def create_snapshot_candidate(store, match_id, kickoff_iso, t_window,
                              as_of=None, registry=None, journal=None,
                              metrics=None, meta=None, repository=None,
                              db_module=None, now=None):
    """Crée un CANDIDAT de snapshot pour un match (T-180/T-60/T-15).

    Retourne un dict explicite. Ne lève jamais vers le scheduler, SAUF
    ValueError sur fenêtre invalide (bug appelant, pas une donnée).
    """
    dbm = db_module or _db
    mets = metrics or _metrics.IngestionMetrics(db_module=dbm)
    if t_window not in WINDOWS:
        raise ValueError(f"t_window inconnue : {t_window!r} (attendu {WINDOWS})")
    as_of = as_of or now_iso()
    now = now or now_iso()

    # ---- identité stable §4 : candidat UNIQUEMENT pour un match connu de
    # la table matches (FK data_snapshots) — jamais orphelin.
    known = dbm.query("SELECT 1 AS x FROM matches WHERE id=%s LIMIT 1",
                      (match_id,), one=True)
    if not known:
        if journal:
            journal.record(event="SNAPSHOT_CANDIDATE_REFUSED",
                           match_id=match_id, status="REFUSED",
                           decision="MATCH_UNKNOWN")
        return {"created": False, "reason": "MATCH_UNKNOWN"}

    # ---- §15 KICKOFF PROTECTION : jamais de candidat après le coup d'envoi
    if kickoff_iso and now[:19] >= kickoff_iso[:19]:
        detail = {"match_id": match_id, "kickoff": kickoff_iso,
                  "attempted_at": now, "t_window": t_window}
        _metrics.raise_alert(_metrics.CRITICAL, "SNAPSHOT_AFTER_KICKOFF",
                             detail, db_module=dbm)
        if journal:
            journal.record(event="SNAPSHOT_CANDIDATE_REFUSED",
                           match_id=match_id, status="REFUSED",
                           decision="KICKOFF_PASSED")
        mets.incr("predictions_blocked")
        return {"created": False, "reason": "KICKOFF_PASSED",
                "alert": "CRITICAL"}

    points = store.query(match_id=match_id, as_of=as_of, only_valid=False)
    usable_known = [p for p in points
                    if p.usable_at(as_of) and not is_unknown(p.value)]
    # §9 — DATA_INSUFFICIENT : jamais de candidat sur zéro donnée réelle
    if not usable_known:
        if journal:
            journal.record(event="SNAPSHOT_CANDIDATE_REFUSED",
                           match_id=match_id, status="DATA_INSUFFICIENT",
                           decision="NO_USABLE_POINT")
        return {"created": False, "reason": "DATA_INSUFFICIENT"}
    payload, fields, qscore = build_candidate_payload(
        points, match_id, as_of, t_window, meta=meta, registry=registry)
    mets.incr("snapshot_candidates")

    # dédup : le même contenu déjà persisté ⇒ aucune nouvelle ligne
    import hashlib
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    phash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if snapshot_hash_exists(match_id, phash, db_module=dbm):
        return {"created": False, "reason": "IDENTICAL", "hash": phash}

    # ---- écriture INSERT-ONLY via la voie 2A déclarée ----------------------
    if repository is None:
        import repository as repository            # pont 2A DÉCLARÉ (§24 WEB-3)
    quality = ("high" if qscore["score"] >= 0.7 else
               "medium" if qscore["score"] >= 0.45 else "low")
    snap = repository.insert_snapshot(match_id, as_of, CANDIDATE_SOURCE,
                                      payload, quality, fields)
    mets.incr("snapshots_created")
    if journal:
        journal.record(event="SNAPSHOT_CANDIDATE_CREATED",
                       match_id=match_id, status="CREATED",
                       decision={"t_window": t_window,
                                 "hash": snap["hash"][:16],
                                 "available_fields": len(fields)})
    return {"created": True, "snapshot_id": snap["id"], "hash": snap["hash"],
            "t_window": t_window, "available_fields": fields,
            "data_quality": quality}


def latest_snapshot(match_id, source, db_module=None):
    dbm = db_module or _db
    r = dbm.query(
        """SELECT * FROM data_snapshots WHERE match_id=%s AND source=%s
           ORDER BY captured_at DESC, id DESC LIMIT 1""",
        (match_id, source), one=True)
    return dict(r) if r else None


def compare_with_2a(match_id, db_module=None):
    """§35 CANARY : compare le dernier snapshot du chemin 2A (source="espn")
    au dernier candidat WEB-4. LECTURE SEULE — jamais d'écrasement."""
    dbm = db_module or _db
    espn = latest_snapshot(match_id, "espn", dbm)
    cand = latest_snapshot(match_id, CANDIDATE_SOURCE, dbm)

    def fields_of(row):
        if not row:
            return []
        try:
            return sorted(json.loads(row.get("available_fields") or "[]"))
        except Exception:
            return []

    f2a, fw4 = fields_of(espn), fields_of(cand)
    return {
        "match_id": match_id,
        "snapshot_2a": ({"id": espn["id"], "hash": espn["payload_hash"],
                         "captured_at": espn["captured_at"],
                         "as_of": espn["as_of_utc"],
                         "quality": espn["data_quality"],
                         "fields": f2a} if espn else None),
        "candidate_web4": ({"id": cand["id"], "hash": cand["payload_hash"],
                            "captured_at": cand["captured_at"],
                            "as_of": cand["as_of_utc"],
                            "quality": cand["data_quality"],
                            "fields": fw4} if cand else None),
        "diff": {
            "fields_only_in_2a": sorted(set(f2a) - set(fw4)),
            "fields_only_in_web4": sorted(set(fw4) - set(f2a)),
            "hash_equal": bool(espn and cand
                               and espn["payload_hash"] == cand["payload_hash"]),
        },
        "policy": "comparaison lecture seule — le candidat n'écrase rien (§35)",
    }


def candidates_for_match(match_id, db_module=None):
    dbm = db_module or _db
    return dbm.rows_to_dicts(dbm.query(
        """SELECT id, captured_at, as_of_utc, payload_hash, data_quality,
                  available_fields
           FROM data_snapshots WHERE match_id=%s AND source=%s
           ORDER BY captured_at, id""", (match_id, CANDIDATE_SOURCE)))


def integrity_scan(limit=None, db_module=None, predsvc=None):
    """§29 — vérifie les hash des prédictions gelées récentes (re-hash SHA-256
    par le service 2A). Toute corruption ⇒ alerte CRITICAL HASH_CHANGED.
    LECTURE SEULE sur le socle. Retourne {checked, corrupted:[...]}."""
    dbm = db_module or _db
    if predsvc is None:
        import prediction_service as predsvc       # pont 2A DÉCLARÉ
    limit = int(limit or WEB4_CONFIG["integrity_scan_limit"])
    rows = dbm.rows_to_dicts(dbm.query(
        "SELECT * FROM predictions ORDER BY frozen_at DESC LIMIT %s",
        (limit,)))
    corrupted = []
    for r in rows:
        try:
            if not predsvc.verify_prediction(r):
                corrupted.append(r["id"])
        except Exception:
            corrupted.append(r["id"])
    if corrupted:
        _metrics.raise_alert(_metrics.CRITICAL, "HASH_CHANGED",
                             {"prediction_ids": corrupted[:10],
                              "count": len(corrupted)}, db_module=dbm)
    return {"checked": len(rows), "corrupted": corrupted}
