# -*- coding: utf-8 -*-
"""
REPOSITORY — couche d'accès aux données (ÉTAPE 2A §2)
=====================================================
DATABASE (db.py) → Repository (ici) → Services → API → Frontend.

Seul module autorisé à manipuler le SQL des entités métier. Les services ne
connaissent que ces fonctions. Toutes les écritures respectent les règles :

- un match = identité stable (source, source_match_id) — §4 ;
- une prédiction gelée n'est JAMAIS modifiée (trigger SQL d'immuabilité) ;
- les nouvelles informations créent de NOUVELLES versions (§8) ;
- les mauvaises prédictions ne sont jamais supprimées (trigger anti-delete) ;
- le cache persistant n'est jamais une source de vérité (§17).
"""

import hashlib
import json
import re
import unicodedata
import uuid

import db


def uid():
    return uuid.uuid4().hex


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def norm_name(s):
    """Nom d'équipe normalisé (clé de secours inter-sources — §4)."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def fallback_key(competition, kickoff, home, away):
    stamp = (kickoff or "")[:13].replace("-", "").replace("T", "")  # aaaammjjhh
    return f"{competition}|{stamp}|{norm_name(home)}|{norm_name(away)}"


# ---------------------------------------------------------------------------
# MATCHES
# ---------------------------------------------------------------------------
def upsert_match(m):
    """Insère ou met à jour un match. Jamais d'écrasement d'un score connu
    par NULL. Retourne l'identifiant BD stable."""
    mid = m["id"]
    now = db.utcnow()
    with db.transaction() as con:
        con.execute(
            """INSERT INTO matches (id, source, source_match_id, fallback_key, competition, season,
                   home_team, away_team, home_team_ext_id, away_team_ext_id, kickoff_time_utc,
                   status, home_score, away_score, home_ht_score, away_ht_score, venue,
                   last_payload_json, last_payload_hash, created_at, updated_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source, source_match_id) DO UPDATE SET
                   kickoff_time_utc=excluded.kickoff_time_utc,
                   status=excluded.status,
                   home_score=COALESCE(excluded.home_score, matches.home_score),
                   away_score=COALESCE(excluded.away_score, matches.away_score),
                   home_ht_score=COALESCE(excluded.home_ht_score, matches.home_ht_score),
                   away_ht_score=COALESCE(excluded.away_ht_score, matches.away_ht_score),
                   venue=COALESCE(NULLIF(excluded.venue,''), matches.venue),
                   last_payload_json=excluded.last_payload_json,
                   last_payload_hash=excluded.last_payload_hash,
                   updated_at=excluded.updated_at,
                   last_seen_at=excluded.last_seen_at""",
            (mid, m["source"], m["source_match_id"], m.get("fallback_key"), m.get("competition"),
             m.get("season"), m["home_team"], m["away_team"], m.get("home_team_ext_id"),
             m.get("away_team_ext_id"), m["kickoff_time_utc"], m.get("status", "UPCOMING"),
             m.get("home_score"), m.get("away_score"), m.get("home_ht_score"),
             m.get("away_ht_score"), m.get("venue", ""), m.get("last_payload_json"),
             m.get("last_payload_hash"), now, now, now))
    return mid


def get_match(mid):
    r = db.query("SELECT * FROM matches WHERE id=%s", (mid,), one=True)
    return dict(r) if r else None


def get_match_by_source(source, source_match_id):
    r = db.query("SELECT * FROM matches WHERE source=%s AND source_match_id=%s",
                 (source, source_match_id), one=True)
    return dict(r) if r else None


def set_evaluation_status(mid, status):
    db.execute("UPDATE matches SET evaluation_status=%s, updated_at=%s WHERE id=%s",
               (status, db.utcnow(), mid))


def count_matches():
    return db.query("SELECT COUNT(*) AS c FROM matches", one=True)["c"]


# ---------------------------------------------------------------------------
# RESULTS
# ---------------------------------------------------------------------------
def insert_result(match_id, home_score, away_score, home_ht, away_ht, final_status, completed_at, source):
    """Insère le résultat officiel capturé (une seule fois ; ignorant les doublons)."""
    now = db.utcnow()
    db.execute(
        """INSERT INTO results (match_id, home_score, away_score, home_ht_score, away_ht_score,
               final_status, completed_at, source, captured_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(match_id) DO NOTHING""",
        (match_id, home_score, away_score, home_ht, away_ht, final_status, completed_at, source, now))
    return now


def get_result(match_id):
    r = db.query("SELECT * FROM results WHERE match_id=%s", (match_id,), one=True)
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# DATA SNAPSHOTS (§5)
# ---------------------------------------------------------------------------
def insert_snapshot(match_id, as_of_utc, source, payload, data_quality, available_fields, source_version=None):
    sid = uid()
    payload_json = canonical_json(payload)
    phash = sha256_text(payload_json)
    now = db.utcnow()
    db.execute(
        """INSERT INTO data_snapshots (id, match_id, captured_at, as_of_utc, source, source_version,
               payload_json, payload_hash, data_quality, available_fields)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sid, match_id, now, as_of_utc, source, source_version, payload_json,
         phash, data_quality, json.dumps(available_fields, ensure_ascii=False)))
    return {"id": sid, "hash": phash, "captured_at": now}


def get_snapshot(sid):
    r = db.query("SELECT * FROM data_snapshots WHERE id=%s", (sid,), one=True)
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# MODEL VERSIONS (§16)
# ---------------------------------------------------------------------------
def register_model(model_name, version, configuration):
    """Enregistre le modèle (idempotent). Une prédiction historique reste
    liée à son modèle d'origine ; un nouveau modèle crée une nouvelle ligne."""
    conf_json = canonical_json(configuration)
    conf_hash = sha256_text(conf_json)[:16]
    now = db.utcnow()
    with db.transaction() as con:
        con.execute(
            """INSERT INTO model_versions (model_name, version, configuration_json, configuration_hash,
                   created_at, active_from)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(model_name, version) DO NOTHING""",
            (model_name, version, conf_json, conf_hash, now, now))
    return conf_hash


def active_model(model_name):
    r = db.query("SELECT * FROM model_versions WHERE model_name=%s AND retired_at IS NULL "
                 "ORDER BY active_from DESC LIMIT 1", (model_name,), one=True)
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# PREDICTIONS (§6/§7/§8) — écriture append-only
# ---------------------------------------------------------------------------
def latest_version_seq(match_id):
    r = db.query("SELECT MAX(version_seq) AS v FROM predictions WHERE match_id=%s", (match_id,), one=True)
    return r["v"] or 0


def latest_snapshot_hash(match_id):
    r = db.query("""SELECT snapshot_hash FROM predictions WHERE match_id=%s
                    ORDER BY version_seq DESC LIMIT 1""", (match_id,), one=True)
    return r["snapshot_hash"] if r else None


def has_snapshot_hash(match_id, snapshot_hash):
    """Ce jeu de données a-t-il DÉJÀ fait l'objet d'une version gelée ?
    (Empêche les versions « ping-pong » A→B→A quand une donnée oscillante —
    p. ex. cotes apparaissant/disparaissant — recréerait du bruit d'historique.)"""
    r = db.query("SELECT 1 AS x FROM predictions WHERE match_id=%s AND snapshot_hash=%s LIMIT 1",
                 (match_id, snapshot_hash), one=True)
    return r is not None


def insert_prediction_version(match_id, rows):
    """Gèle une nouvelle version : INSERT uniquement, jamais d'UPDATE.
    `rows` = liste de dicts (une par famille de marché) déjà calculés par le service.
    frozen_at/created_at viennent du SERVICE (même instant que le hash — §15)."""
    with db.transaction() as con:
        for r in rows:
            con.execute(
                """INSERT INTO predictions (id, match_id, version_seq, prediction_created_at,
                       prediction_effective_at, kickoff_time_utc, market, selection,
                       raw_probability, published_probability, fair_odds, bookmaker_odds,
                       distribution_json, model_name, model_version, snapshot_id, snapshot_hash,
                       prediction_hash, prediction_status, frozen_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'PREDICTION_FROZEN', ?, ?)""",
                (r["id"], match_id, r["version_seq"], r["frozen_at"], r["frozen_at"],
                 r["kickoff_time_utc"], r["market"],
                 r["selection"], r["raw_probability"], r["published_probability"], r.get("fair_odds"),
                 r.get("bookmaker_odds"), r["distribution_json"], r["model_name"], r["model_version"],
                 r["snapshot_id"], r["snapshot_hash"], r["prediction_hash"],
                 r["frozen_at"], r["frozen_at"]))


def evaluation_version(match_id, kickoff_utc):
    """Version de RÉFÉRENCE pour l'évaluation : la plus récente gelée AVANT le
    coup d'envoi (§7 — le gel de T-15 est simplement la version la plus proche
    du coup d'envoi, tous les gels restant antérieurs)."""
    r = db.query("""SELECT version_seq FROM predictions
                    WHERE match_id=%s AND frozen_at <= %s
                    ORDER BY version_seq DESC LIMIT 1""",
                 (match_id, kickoff_utc), one=True)
    return r["version_seq"] if r else None


def predictions_for_version(match_id, version_seq):
    return db.rows_to_dicts(db.query(
        "SELECT * FROM predictions WHERE match_id=%s AND version_seq=%s ORDER BY market",
        (match_id, version_seq)))


def predictions_history(match_id):
    """Toutes les versions gelées d'un match (§14 historique immuable)."""
    return db.rows_to_dicts(db.query(
        "SELECT * FROM predictions WHERE match_id=%s ORDER BY version_seq, market", (match_id,)))


def get_prediction(pid):
    r = db.query("SELECT * FROM predictions WHERE id=%s", (pid,), one=True)
    return dict(r) if r else None


def mark_prediction_status(pid, status):
    """Seule mise à jour autorisée sur une prédiction (cycle de vie)."""
    db.execute("UPDATE predictions SET prediction_status=%s WHERE id=%s", (status, pid))


def insert_prediction_result(pid, outcome, won, settled_at, brier, logloss):
    now = db.utcnow()
    db.execute(
        """INSERT INTO prediction_results (prediction_id, actual_outcome, won, settled_at,
               brier_score, log_loss, evaluated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(prediction_id) DO NOTHING""",
        (pid, outcome, won, settled_at, brier, logloss, now))


def settled_count():
    r = db.query("""SELECT COUNT(DISTINCT p.match_id) AS c FROM predictions p
                    JOIN prediction_results r ON r.prediction_id=p.id""", one=True)
    return r["c"]


def frozen_count():
    r = db.query("SELECT COUNT(*) AS c FROM predictions", one=True)
    return r["c"]


def evaluation_rows(where="", params=(), limit=None):
    """Lignes gelées + résultats pour les métriques (§12/§13)."""
    sql = """SELECT p.id, p.match_id, p.market, p.selection, p.published_probability,
                    p.raw_probability, p.bookmaker_odds, p.distribution_json, p.model_name,
                    p.model_version, p.version_seq, p.frozen_at, p.kickoff_time_utc, p.prediction_status,
                    m.competition, m.home_team, m.away_team,
                    r.actual_outcome, r.won, r.brier_score, r.log_loss, r.settled_at
             FROM predictions p
             JOIN matches m ON m.id = p.match_id
             LEFT JOIN prediction_results r ON r.prediction_id = p.id """
    sql += where + " ORDER BY p.frozen_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.rows_to_dicts(db.query(sql, params))


def evaluation_versions_filter():
    """(match_id, version_seq) des versions de référence évaluables :
    dernière version gelée avant coup d'envoi pour chaque match réglé."""
    return db.rows_to_dicts(db.query("""
        SELECT p.match_id, p.version_seq FROM predictions p
        JOIN matches m ON m.id=p.match_id
        WHERE p.version_seq = (SELECT MAX(p2.version_seq) FROM predictions p2
                               WHERE p2.match_id=p.match_id AND p2.frozen_at <= p2.kickoff_time_utc)"""))


def evaluation_rows_latest(limit=None):
    """Toutes les lignes des versions gelées DE RÉFÉRENCE (dernière version
    avant coup d'envoi) avec leur règlement éventuel — base unique et honnête
    de toutes les métriques publiques."""
    sql = """WITH ev AS (SELECT match_id, MAX(version_seq) AS seq FROM predictions
                         WHERE frozen_at <= kickoff_time_utc GROUP BY match_id)
             SELECT p.id, p.match_id, p.market, p.selection, p.published_probability,
                    p.raw_probability, p.bookmaker_odds, p.distribution_json, p.model_name,
                    p.model_version, p.version_seq, p.frozen_at, p.kickoff_time_utc, p.prediction_status,
                    m.competition, m.home_team, m.away_team,
                    r.actual_outcome, r.won, r.brier_score, r.log_loss, r.settled_at
             FROM predictions p
             JOIN ev ON ev.match_id=p.match_id AND ev.seq=p.version_seq
             JOIN matches m ON m.id=p.match_id
             LEFT JOIN prediction_results r ON r.prediction_id=p.id
             ORDER BY p.frozen_at DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.rows_to_dicts(db.query(sql))


# ---------------------------------------------------------------------------
# ODDS (§3)
# ---------------------------------------------------------------------------
def insert_odds(match_id, captured_at, source, entries):
    """Enregistre les cotes capturées (bookmaker, marché, sélection, cote).
    Déduplique à (match, bookmaker, marché, sélection, cote) près."""
    rows = []
    seen = set()
    for e in entries:
        key = (match_id, e.get("bookmaker"), e["market"], e["selection"], e["odds"])
        if key in seen:
            continue
        seen.add(key)
        rows.append((uid(), match_id, e.get("bookmaker"), e["market"], e["selection"],
                     float(e["odds"]), captured_at, source))
    if not rows:
        return 0
    existing = {tuple(r) for r in db.query(
        """SELECT match_id, bookmaker, market, selection, odds FROM odds
           WHERE match_id=%s AND captured_at > datetime('now', '-2 day')""", (match_id,))}
    rows = [r for r in rows
            if (r[1], r[2], r[3], r[4], float(r[5])) not in
            {(x[0], x[1], x[2], x[3], float(x[4])) for x in existing}]
    if rows:
        db.executemany(
            """INSERT INTO odds (id, match_id, bookmaker, market, selection, odds, captured_at, source)
               VALUES (?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def latest_moneyline(match_id):
    """Dernière moneyline 1X2 connue (probas dénormalisées) — pour affichage/métriques."""
    rows = db.rows_to_dicts(db.query(
        """SELECT selection, odds, bookmaker, captured_at FROM odds
           WHERE match_id=%s AND market='1X2' ORDER BY captured_at DESC LIMIT 3""", (match_id,)))
    if len(rows) < 3:
        return None
    return rows


# ---------------------------------------------------------------------------
# CACHE PERSISTANT (§17/§18) — jamais une source de vérité
# ---------------------------------------------------------------------------
def cache_save(key, payload_json, ttl_seconds):
    import time
    now = time.time()
    db.execute(
        """INSERT INTO cache_store (key, payload_json, updated_at, expires_at)
           VALUES (?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json,
               updated_at=excluded.updated_at, expires_at=excluded.expires_at""",
        (key, payload_json, now, now + ttl_seconds))


def cache_load_all():
    """Restaure au démarrage les entrées non expirées (§18)."""
    import time
    now = time.time()
    out = {}
    for r in db.query("SELECT key, payload_json, expires_at FROM cache_store WHERE expires_at > %s", (now,)):
        try:
            out[r["key"]] = (r["expires_at"], json.loads(r["payload_json"]))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# JOURNAL D'AUDIT (§14)
# ---------------------------------------------------------------------------
def log_event(event, match_id=None, prediction_id=None, detail=None):
    db.execute(
        "INSERT INTO prediction_events (prediction_id, match_id, event, detail_json, at_utc) VALUES (?,?,?,?,?)",
        (prediction_id, match_id, event,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None, db.utcnow()))


def events_for_match(match_id):
    return db.rows_to_dicts(db.query(
        "SELECT * FROM prediction_events WHERE match_id=%s ORDER BY at_utc, id", (match_id,)))


def events_for_prediction(prediction_id):
    return db.rows_to_dicts(db.query(
        "SELECT * FROM prediction_events WHERE prediction_id=%s ORDER BY at_utc, id", (prediction_id,)))


def has_event(match_id, event):
    r = db.query("SELECT 1 AS x FROM prediction_events WHERE match_id=%s AND event=%s LIMIT 1",
                 (match_id, event), one=True)
    return r is not None
