# -*- coding: utf-8 -*-
"""
COUCHE DE PERSISTANCE — PronoFoot Live (ÉTAPE 2A)
=================================================
- SQLite en mode WAL (journal Write-Ahead : lectures concurrentes rapides,
  robuste au crash, zéro dépendance externe — parfait pour l'offre gratuite).
- TOUT le SQL de l'application vit ici et dans repository.py : le code métier
  ne contient aucune requête dispersée (architecture Repository — §2).
- Les requêtes sont écrites en style psycopg (%s) puis traduites pour SQLite :
  un futur passage à PostgreSQL = changer _connect() + _ph(), sans réécrire
  l'application (§20).
- Migrations versionnées (schema_migrations) : le schéma évolue uniquement par
  migrations reproductibles (§19).

GARANTIE D'INTÉGRITÉ : un trigger SQLite rend toute prédiction gelée
IMMUTABLE au niveau de la base elle-même — même un bug du code applicatif
ne peut plus modifier sa probabilité, sa sélection, son modèle, son snapshot
ou son timestamp (§6 « UNE PRÉDICTION PUBLIÉE EST IMMUTABLE »).
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_DB_PATH = None
_LOCK = threading.RLock()
_INITED = False


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def db_path():
    global _DB_PATH
    if _DB_PATH is None:
        base = os.path.dirname(os.path.abspath(__file__))
        _DB_PATH = os.environ.get("PRONOFOOT_DB") or os.path.join(base, "data", "pronofoot.db")
    return _DB_PATH


def _connect(path):
    con = sqlite3.connect(path, timeout=20, isolation_level=None)  # autocommit ; tx explicites
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def _ph(sql):
    """Style psycopg (%s) → placeholder SQLite (?). Point d'adaptation PostgreSQL."""
    return sql.replace("%s", "?")


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def query(sql, params=(), one=False):
    with _LOCK:
        con = _connect(db_path())
        try:
            cur = con.execute(_ph(sql), params)
            res = cur.fetchall()
        finally:
            con.close()
    if one:
        return res[0] if res else None
    return res


def execute(sql, params=()):
    with _LOCK:
        con = _connect(db_path())
        try:
            cur = con.execute(_ph(sql), params)
            return cur.lastrowid
        finally:
            con.close()


def executemany(sql, seq):
    with _LOCK:
        con = _connect(db_path())
        try:
            con.executemany(_ph(sql), seq)
        finally:
            con.close()


@contextmanager
def transaction():
    """Transaction IMMEDIATE : un seul écrivain à la fois, toutes les lectures
    concurrentes restent possibles grâce au WAL."""
    with _LOCK:
        con = _connect(db_path())
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            con.close()


# ---------------------------------------------------------------------------
# MIGRATIONS (§19) — (version, nom, [instructions SQL])
# ---------------------------------------------------------------------------
SCHEMA_V1 = [
    # IDENTITÉ STABLE DES MATCHS (§4) : (source, source_match_id) unique ;
    # clé normalisée de secours competition+date+équipes pour le futur
    # multi-sources (déduplication inter-sources — non utilisée en 2A).
    """CREATE TABLE matches (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_match_id TEXT NOT NULL,
        fallback_key TEXT,
        competition TEXT,
        season TEXT,
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        home_team_ext_id TEXT,
        away_team_ext_id TEXT,
        kickoff_time_utc TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'UPCOMING',
        home_score INTEGER,
        away_score INTEGER,
        home_ht_score INTEGER,
        away_ht_score INTEGER,
        venue TEXT,
        evaluation_status TEXT NOT NULL DEFAULT 'PENDING',
        last_payload_json TEXT,
        last_payload_hash TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE (source, source_match_id)
    )""",
    "CREATE INDEX idx_matches_kickoff ON matches(kickoff_time_utc)",
    "CREATE INDEX idx_matches_status ON matches(status)",
    "CREATE INDEX idx_matches_competition ON matches(competition)",
    """CREATE TABLE results (
        match_id TEXT PRIMARY KEY REFERENCES matches(id),
        home_score INTEGER,
        away_score INTEGER,
        home_ht_score INTEGER,
        away_ht_score INTEGER,
        final_status TEXT,
        completed_at TEXT,
        source TEXT,
        captured_at TEXT NOT NULL
    )""",
    # SNAPSHOTS (§5) : toutes les données connues du modèle à l'instant T.
    """CREATE TABLE data_snapshots (
        id TEXT PRIMARY KEY,
        match_id TEXT NOT NULL REFERENCES matches(id),
        captured_at TEXT NOT NULL,
        as_of_utc TEXT NOT NULL,
        source TEXT,
        source_version TEXT,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        data_quality TEXT,
        available_fields TEXT
    )""",
    "CREATE INDEX idx_snapshots_match ON data_snapshots(match_id)",
    # VERSIONNAGE DES MODÈLES (§16)
    """CREATE TABLE model_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_name TEXT NOT NULL,
        version TEXT NOT NULL,
        configuration_json TEXT,
        configuration_hash TEXT,
        created_at TEXT NOT NULL,
        active_from TEXT,
        retired_at TEXT,
        UNIQUE (model_name, version)
    )""",
    # PRÉDICTIONS GELÉES (§6/§7) : une ligne par famille de marché et par
    # version. prediction_status ∈ {PREDICTION_FROZEN, SETTLED, VOID} ;
    # PREDICTION_UPDATED est un état DÉRIVÉ (version remplacée) exposé par
    # l'API — jamais par modification des valeurs gelées.
    """CREATE TABLE predictions (
        id TEXT PRIMARY KEY,
        match_id TEXT NOT NULL REFERENCES matches(id),
        version_seq INTEGER NOT NULL,
        prediction_created_at TEXT NOT NULL,
        prediction_effective_at TEXT NOT NULL,
        kickoff_time_utc TEXT NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        raw_probability REAL NOT NULL,
        published_probability REAL NOT NULL,
        fair_odds REAL,
        bookmaker_odds REAL,
        distribution_json TEXT,
        model_name TEXT NOT NULL,
        model_version TEXT NOT NULL,
        snapshot_id TEXT NOT NULL REFERENCES data_snapshots(id),
        snapshot_hash TEXT NOT NULL,
        prediction_hash TEXT NOT NULL,
        prediction_status TEXT NOT NULL DEFAULT 'PREDICTION_FROZEN',
        frozen_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (match_id, version_seq, market)
    )""",
    "CREATE INDEX idx_predictions_match ON predictions(match_id)",
    "CREATE INDEX idx_predictions_status ON predictions(prediction_status)",
    "CREATE INDEX idx_predictions_frozen ON predictions(frozen_at)",
    """CREATE TABLE prediction_results (
        prediction_id TEXT PRIMARY KEY REFERENCES predictions(id),
        actual_outcome TEXT,
        won INTEGER,
        settled_at TEXT NOT NULL,
        brier_score REAL,
        log_loss REAL,
        evaluated_at TEXT NOT NULL
    )""",
    """CREATE TABLE odds (
        id TEXT PRIMARY KEY,
        match_id TEXT NOT NULL REFERENCES matches(id),
        bookmaker TEXT,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        odds REAL NOT NULL,
        captured_at TEXT NOT NULL,
        source TEXT
    )""",
    "CREATE INDEX idx_odds_match ON odds(match_id)",
    # JOURNAL D'AUDIT : chaque événement de vie d'une prédiction (création de
    # version, marque T-15, règlement...) est tracé SANS modifier les valeurs.
    """CREATE TABLE prediction_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id TEXT,
        match_id TEXT,
        event TEXT NOT NULL,
        detail_json TEXT,
        at_utc TEXT NOT NULL
    )""",
    "CREATE INDEX idx_pevents_pred ON prediction_events(prediction_id)",
    "CREATE INDEX idx_pevents_match ON prediction_events(match_id)",
    # CACHE PERSISTANT (§17/§18) : la RAM reste un cache ; au redémarrage,
    # les caches lourds (stats d'équipes, classements) sont restaurés d'ici.
    """CREATE TABLE cache_store (
        key TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        updated_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )""",
    # TRIGGER D'IMMUTABILITÉ (§6) : interdiction ABSOLUE, au niveau SQL, de
    # modifier les valeurs d'une prédiction gelée. Seuls les champs de cycle
    # de vie (prediction_status) peuvent évoluer, et uniquement vers
    # SETTLED ou VOID.
    """CREATE TRIGGER trg_predictions_immutable BEFORE UPDATE ON predictions
       FOR EACH ROW
       WHEN NEW.market <> OLD.market
         OR NEW.selection <> OLD.selection
         OR NEW.raw_probability <> OLD.raw_probability
         OR NEW.published_probability <> OLD.published_probability
         OR (NEW.fair_odds IS NOT OLD.fair_odds)
         OR NEW.model_name <> OLD.model_name
         OR NEW.model_version <> OLD.model_version
         OR NEW.snapshot_id <> OLD.snapshot_id
         OR NEW.snapshot_hash <> OLD.snapshot_hash
         OR NEW.prediction_hash <> OLD.prediction_hash
         OR NEW.frozen_at <> OLD.frozen_at
         OR NEW.kickoff_time_utc <> OLD.kickoff_time_utc
       BEGIN
         SELECT RAISE(ABORT, 'PREDICTION GELEE IMMUABLE : modification interdite');
       END""",
    "CREATE TRIGGER trg_predictions_no_delete BEFORE DELETE ON predictions BEGIN SELECT RAISE(ABORT, 'PREDICTION GELEE IMMUABLE : suppression interdite'); END",
    "CREATE TRIGGER trg_results_no_delete BEFORE DELETE ON results BEGIN SELECT RAISE(ABORT, 'RESULTAT OFFICIEL : suppression interdite'); END",
]

# ---------------------------------------------------------------------------
# MIGRATION v2 (ÉTAPE 2B.WEB-4) — 100 % ADDITIVE : nouvelles tables uniquement.
# Aucune table 2A n'est modifiée ; les triggers d'immuabilité sont intacts.
# La sauvegarde fichier (backup.py) couvre automatiquement ces tables (§24).
# ---------------------------------------------------------------------------
SCHEMA_V2 = [
    # §3/§4 — CACHE HTTP PERSISTANT. Jamais une source de vérité : une entrée
    # peut expirer ou être REMPLACÉE (contrairement aux snapshots). AUCUNE URL
    # n'est stockée en clair (url_hash seulement) : impossible de fuiter une
    # clé présente dans une query-string. Aucun header n'est stocké (§38).
    """CREATE TABLE web_cache (
        cache_key TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        url_hash TEXT NOT NULL,
        data_type TEXT,
        response_payload BLOB NOT NULL,
        status INTEGER,
        retrieved_at TEXT NOT NULL,
        expires_at REAL,
        content_type TEXT,
        response_size INTEGER,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_web_cache_source ON web_cache(source_id)",
    "CREATE INDEX idx_web_cache_exp ON web_cache(expires_at)",
    # §5/§6 — POINT-IN-TIME STORE PERSISTANT (append-only) : chaque nouvelle
    # observation = une NOUVELLE ligne (versionnage temporel). dedupe_key
    # bloque uniquement les DOUBLONS EXACTS, jamais les versions distinctes.
    """CREATE TABLE web_datapoints (
        id TEXT PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        match_id TEXT,
        team_id TEXT,
        player_id TEXT,
        competition_id TEXT,
        data_type TEXT NOT NULL,
        level TEXT NOT NULL,
        value_json TEXT,
        is_unknown INTEGER NOT NULL DEFAULT 0,
        source_id TEXT NOT NULL,
        source_url TEXT,
        retrieved_at TEXT NOT NULL,
        published_at TEXT,
        effective_at TEXT,
        confidence TEXT,
        valid INTEGER NOT NULL DEFAULT 1,
        issues_json TEXT,
        checksum TEXT NOT NULL,
        derivation_method TEXT,
        model_version TEXT,
        inputs_json TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_wdp_match ON web_datapoints(match_id, data_type)",
    "CREATE INDEX idx_wdp_retrieved ON web_datapoints(retrieved_at)",
    "CREATE INDEX idx_wdp_source ON web_datapoints(source_id)",
    # §23/§28 — JOURNAL DE RECHERCHE PERSISTANT (whitelist de colonnes :
    # aucune clé interdite ne peut exister ici, par construction — §38).
    """CREATE TABLE web_research_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        event TEXT,
        match_id TEXT,
        source TEXT,
        data_type TEXT,
        host TEXT,
        status TEXT,
        latency_ms REAL,
        bytes INTEGER,
        cache_hit INTEGER,
        records_found INTEGER,
        confidence TEXT,
        freshness TEXT,
        decision_json TEXT,
        error TEXT
    )""",
    "CREATE INDEX idx_wre_match ON web_research_events(match_id)",
    "CREATE INDEX idx_wre_source ON web_research_events(source)",
    "CREATE INDEX idx_wre_ts ON web_research_events(timestamp)",
    # §8 — CYCLES D'INGESTION REGROUPÉS (jamais 50 jobs par match).
    """CREATE TABLE web_ingestion_cycles (
        cycle_id TEXT PRIMARY KEY,
        trigger TEXT NOT NULL,
        phase TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        match_count INTEGER DEFAULT 0,
        request_count INTEGER DEFAULT 0,
        cache_hits INTEGER DEFAULT 0,
        errors_json TEXT,
        duration_ms INTEGER,
        status TEXT,
        detail_json TEXT
    )""",
    "CREATE INDEX idx_wic_started ON web_ingestion_cycles(started_at)",
    # §17/§18 — ENTITY MAP PERSISTANTE (seuils WEB-1 : AUTO ≥0.95,
    # JOURNALIZED 0.80–0.95, sinon UNKNOWN jamais auto-mappé).
    """CREATE TABLE web_entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        internal_key TEXT NOT NULL,
        source_id TEXT NOT NULL,
        external_id TEXT NOT NULL,
        external_name TEXT,
        confidence REAL,
        status TEXT NOT NULL,
        decided_by TEXT,
        decided_at TEXT NOT NULL,
        evidence_json TEXT,
        UNIQUE (entity_type, source_id, external_id)
    )""",
    "CREATE INDEX idx_we_internal ON web_entities(entity_type, internal_key)",
    # §19 — RÉFÉRENTIEL STADES MINIMAL : coordonnées OBLIGATOIRES et
    # sourcées (jamais de coordonnées de pays approximatives).
    """CREATE TABLE web_venues (
        venue_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        city TEXT,
        country TEXT,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        source TEXT NOT NULL,
        confidence REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    # §28 — COMPTEURS D'OBSERVABILITÉ persistants (qualité DONNÉES, jamais
    # métriques du modèle — voir quality_metrics.assert_no_model_metrics).
    """CREATE TABLE web_metrics (
        key TEXT PRIMARY KEY,
        value REAL NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""",
    # §37 — ÉTAT DU SCHEDULER : survit au restart (Render free suspend).
    """CREATE TABLE web_scheduler_state (
        key TEXT PRIMARY KEY,
        value_json TEXT,
        updated_at TEXT NOT NULL
    )""",
    # §29 — ALERTES INTERNES (WARNING / ERROR / CRITICAL).
    """CREATE TABLE web_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT NOT NULL,
        code TEXT NOT NULL,
        detail_json TEXT,
        at_utc TEXT NOT NULL
    )""",
    "CREATE INDEX idx_wa_at ON web_alerts(at_utc)",
]

# ---------------------------------------------------------------------------
# MIGRATION v3 (ÉTAPE 2C — EXPERIMENTAL) — 100 % ADDITIVE.
# Tables du moteur SHADOW 2C. Aucune table v1/v2 n'est modifiée ; les
# triggers d'immuabilité 2A sont intacts. 2C reste invisible du frontend
# et ne remplace jamais 2A (§32/§34 du cahier 2C).
# ---------------------------------------------------------------------------
SCHEMA_V3 = [
    # §24 — REGISTRE DES MODÈLES 2C (statuts EXPERIMENTAL→…→RETIRED)
    """CREATE TABLE model2c_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_id TEXT NOT NULL,
        version TEXT NOT NULL,
        features_json TEXT,
        feature_version TEXT,
        training_window_json TEXT,
        calibration_version TEXT,
        dataset_hash TEXT,
        code_hash TEXT,
        metrics_json TEXT,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'EXPERIMENTAL',
        UNIQUE (model_id, version)
    )""",
    # §25 — PRÉDICTIONS SHADOW (append-only, invisibles publiquement)
    """CREATE TABLE predictions_2c_shadow (
        id TEXT PRIMARY KEY,
        match_id TEXT NOT NULL,
        prediction_time TEXT NOT NULL,
        model_version TEXT NOT NULL,
        model_id TEXT NOT NULL,
        raw_probabilities TEXT NOT NULL,
        calibrated_probabilities TEXT,
        features_hash TEXT,
        dataset_hash TEXT,
        data_quality_json TEXT,
        sample_size INTEGER DEFAULT 0,
        calibration_version TEXT,
        code_hash TEXT,
        status TEXT NOT NULL DEFAULT 'EXPERIMENTAL',
        shadow_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_p2cs_match ON predictions_2c_shadow(match_id)",
    "CREATE INDEX idx_p2cs_model ON predictions_2c_shadow(model_id)",
    # §4 — FEATURE SNAPSHOTS (vecteur par match, hashé, versionné, traçable)
    """CREATE TABLE features_2c (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT NOT NULL,
        team_id TEXT,
        feature_name TEXT NOT NULL,
        feature_value REAL,
        feature_json TEXT,
        as_of TEXT NOT NULL,
        source TEXT,
        snapshot_id TEXT,
        feature_version TEXT NOT NULL,
        feature_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_f2c_match ON features_2c(match_id)",
    # APPEND-ONLY : une prédiction shadow ne se modifie ni ne se supprime
    """CREATE TRIGGER trg_p2cs_no_update BEFORE UPDATE ON predictions_2c_shadow
       BEGIN SELECT RAISE(ABORT, 'SHADOW 2C APPEND-ONLY : modification interdite'); END""",
    """CREATE TRIGGER trg_p2cs_no_delete BEFORE DELETE ON predictions_2c_shadow
       BEGIN SELECT RAISE(ABORT, 'SHADOW 2C APPEND-ONLY : suppression interdite'); END""",
    """CREATE TRIGGER trg_f2c_no_delete BEFORE DELETE ON features_2c
       BEGIN SELECT RAISE(ABORT, 'FEATURE 2C APPEND-ONLY : suppression interdite'); END""",
]

MIGRATIONS = [
    (1, "schema initial : persistance + predictions gelees + anti-fuite", SCHEMA_V1),
    (2, "web4 : ingestion persistante (cache+PIT+journal+cycles+entites)", SCHEMA_V2),
    (3, "2c experimental : shadow + registry + features (additif)", SCHEMA_V3),
]


def migrate():
    """Applique les migrations manquantes, dans une transaction, de façon
    idempotente et reproductible (jamais de modification manuelle — §19)."""
    p = db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with _LOCK:
        con = _connect(p)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                             version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT NOT NULL)""")
            done = {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
            for version, name, stmts in MIGRATIONS:
                if version in done:
                    continue
                for s in stmts:
                    con.execute(s)
                con.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, %s)"
                            .replace("%s", "?"), (version, name, utcnow()))
                print(f"[db] migration v{version} appliquée : {name}")
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            con.close()


def init(path=None, reset=False):
    """Initialise la couche de persistance (idempotent). `path` permet aux
    tests d'utiliser une base temporaire isolée."""
    global _DB_PATH, _INITED
    with _LOCK:
        if path:
            _DB_PATH = path
        if _INITED and not reset and not path:
            return db_path()
        migrate()
        _INITED = True
        return db_path()


def migration_version():
    row = query("SELECT MAX(version) AS v FROM schema_migrations", one=True)
    return row["v"] if row and row["v"] else 0


def integrity_check():
    row = query("PRAGMA integrity_check", one=True)
    return row and list(row)[0] == "ok"


def table_counts():
    out = {}
    for t in ("matches", "results", "data_snapshots", "predictions",
              "prediction_results", "odds", "model_versions", "prediction_events"):
        try:
            out[t] = query(f"SELECT COUNT(*) AS c FROM {t}", one=True)["c"]
        except Exception:
            out[t] = -1
    return out
