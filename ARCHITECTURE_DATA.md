# ARCHITECTURE DES DONNÉES — PronoFoot Live 3.0 (ÉTAPE 2A)

> **Principe fondateur :** la mémoire vive n'est plus qu'un **cache**.
> La vérité est dans la base. Toute prédiction publiée est **gelée, horodatée,
> hachée et immuable** — auditable à vie, même par un non-développeur via les
> endpoints publics.

```
ESPN (source unique, gratuite, sans clé)
   │  scoreboard / summary / standings
   ▼
INGESTION (app.py : feed_loop 25 s / 120 s, live_detail_loop, prematch_detail_loop, warm_stats)
   │  parse_event / parse_summary — horodatage de chaque capture
   ▼
VALIDATION + NORMALISATION (app.py : persist_match — identité stable, dédup)
   ▼
SERVICES (prediction_service.py : publication gelée, T-15, règlement, métriques)
   ▼
REPOSITORY (repository.py — TOUT le SQL métier, aucun SQL ailleurs)
   ▼
DATABASE (db.py : SQLite WAL, migrations versionnées — PostgreSQL-ready)
   ▼
API (/api/feed, /api/stream SSE, /api/predictions/*, /api/matches/*/predictions)
   ▼
FRONTEND (static/index.html — affichage gelé, jamais de recalcul)
```

Légende des flux : **en écriture** seules les flèches INGESTION→…→DATABASE circulent ;
la lecture frontend repasse par le cache RAM (STATE) et, pour les prédictions
live/terminées, par une **relecture de la persistance** (jamais un recalcul).

---

## 1. Tables (migration v1)

### `matches` — identité stable (§4)
| colonne | rôle |
|---|---|
| `id` | `espn:{competition}:{eventId}` (clé primaire) |
| `source`, `source_match_id` | **UNIQUE(source, source_match_id)** — jamais équipe+équipe seul |
| `fallback_key` | clé normalisée de secours `competition|aaaammjjhh|home|away` (dédoublonnage futur multi-sources) |
| `competition`, `season`, `home_team`, `away_team`, `home/away_team_ext_id` | dimensions |
| `kickoff_time_utc` | coup d'envoi UTC (horodatage source) |
| `status` | état DU MATCH : `UPCOMING / LIVE / FINISHED` (≠ état de la prédiction — §22) |
| `home_score`, `away_score`, `home_ht_score`, `away_ht_score` | dernier état connu |
| `evaluation_status` | `PENDING / SETTLED / NOT_EVALUABLE / VOID` |
| `last_payload_json/hash`, `created_at`, `updated_at`, `last_seen_at` | traçabilité ingestion |

### `results` — résultat officiel capturé UNE fois
`match_id (PK, FK)`, `home_score`, `away_score`, `home_ht_score`, `away_ht_score`,
`final_status`, `completed_at`, `source`, `captured_at` (horodatage de capture).
**Trigger SQL : suppression interdite.**

### `data_snapshots` (§5 — CRITIQUE) — « ce que le modèle savait exactement »
`id`, `match_id (FK)`, `captured_at`, `as_of_utc`, `source`, `source_version`,
`payload_json` (features + sortie complète du modèle), `payload_hash` (SHA-256),
`data_quality` (`full/partial`), `available_fields` (JSON : `team_stats`,
`league_avgs`, `injuries?`, `lineups?`, `odds?`).
**Le payload ne contient volontairement AUCUN timestamp** : son hash sert à
détecter tout changement réel de données (⇒ nouvelle version) et à empêcher les
versions « ping-pong ».

### `predictions` (§6/§7) — une ligne par famille de marché et par version
`id`, `match_id (FK)`, `version_seq`, `prediction_created_at`,
`prediction_effective_at`, `kickoff_time_utc`, `market`
(`1N2 | DC | O1.5 | O2.5 | O3.5 | BTTS`), `selection`,
`raw_probability`, `published_probability` (= raw tant que non calibré — §28),
`fair_odds`, `bookmaker_odds` (cote réelle capturée au gel, si disponible),
`distribution_json` (**distribution complète** : `{1,N,2}` pour 1N2, etc. — §13),
`model_name`, `model_version`, `snapshot_id (FK)`, `snapshot_hash`,
`prediction_hash` (SHA-256), `prediction_status`
(`PREDICTION_FROZEN → SETTLED | VOID`), `frozen_at`, `created_at`.
`UNIQUE(match_id, version_seq, market)`.

> **IMMUTABILITÉ GARANTIE PAR LA BASE** (pas juste par le code) :
> trigger `trg_predictions_immutable` — tout UPDATE touchant une valeur
> (marché, sélection, probabilités, modèle, snapshot, hash, frozen_at,
> kickoff) est **rejeté** ; trigger `trg_predictions_no_delete` — suppression
> interdite. Seul `prediction_status` peut évoluer (cycle de vie).
> L'état `PREDICTION_UPDATED` demandé au §22 est un état **dérivé**
> (une version plus récente existe) exposé par l'API — jamais une écriture
> destructrice sur l'ancienne version.

### `prediction_results`
`prediction_id (PK, FK)`, `actual_outcome`, `won (1/0)`, `settled_at`,
`brier_score` (multi-classes pour 1N2, binaire sinon), `log_loss`, `evaluated_at`.

### `odds`
`id`, `match_id (FK)`, `bookmaker`, `market`, `selection`, `odds` (décimale réelle),
`captured_at`, `source`. Dédupliqué ; **absence de cote = absence honnête**
(ROI calculé uniquement sur le sous-ensemble coté — jamais simulé).

### `model_versions` (§16)
`id`, `model_name`, `version`, `configuration_json`, `configuration_hash`,
`created_at`, `active_from`, `retired_at`, UNIQUE(model_name, version).
Aujourd'hui : `poisson 1.0.0` (config complète hachée, flag `calibrated: false`).
Demain : `dixon_coles 1.1.0`, `ensemble 2.0.0` — l'historique restera lié à
`poisson 1.0.0`.

### `prediction_events` — journal d'audit append-only
`prediction_frozen` (avec version remplacée le cas échéant), `t15_reference`
(version en vigueur à T-15), `result_captured`, `settled`, `not_evaluable`, `void`.

### `cache_store` (§17/§18)
Caches lourds persistés (stats d'équipes ~150 j, classements) avec expiration —
**restauration au démarrage** pour éviter la reconstruction de plusieurs minutes.
Jamais utilisé comme source de vérité métier.

### `schema_migrations` (§19)
`version`, `name`, `applied_at` — appliquées en transaction, idempotentes.

---

## 2. Cycle de vie d'une prédiction

Voir `PREDICTION_LIFECYCLE.md`. Point clé : **évaluation = version marquée
« référence T-15 »** si elle existe, sinon dernière version dont
`frozen_at <= kickoff_time_utc`. Jamais une version gelée après le coup d'envoi
(`publish_match()` refuse `now >= kickoff` — §9).

## 3. Anti-fuite (§9/§10)

1. `publish_match()` → `None` si le coup d'envoi est passé (**aucune** création).
2. `enrich_match()` — `post` : suppression totale de l'ancien recalcul ;
   soit relecture de la gelée + verdict, soit `NOT_EVALUABLE`.
3. Les features du snapshot ne contiennent que des données capturées avant
   `as_of_utc` (historique des matchs terminés — fenêtre glissante ~150 j).
4. Le règlement n'écrit que dans `prediction_results` (+ statut) sans jamais
   toucher aux valeurs gelées.
5. Test automatique §24 : modifier le futur dans la base ne change **rien** à
   une prédiction passée (sinon `test_14` échoue).

## 4. Hash / intégrité (§15)

- `snapshot.payload_hash = SHA-256(payload_json canonique)` ;
- `prediction.prediction_hash = SHA-256(match_id, version_seq, market, selection,
  raw_probability, published_probability, model, snapshot_hash, frozen_at)` ;
- vérification publique : `GET /api/predictions/{id}` → `integrity: OK|CORROMPU`.

## 5. Cache ≠ base (§17/§18) & démarrage

```
backup.restore_if_needed()  →  db.init() (migrations)
→ register_model  →  restauration cache_store (stats, classements)
→ workers (feed, live_detail, prematch_detail, warm, keep-awake, backup)
→ Live + SSE
```

- Durabilité sur hébergement gratuit (disque éphémère) : sauvegarde périodique
  de la base compressée en **asset de Release GitHub** (`backup.py`, activé par
  `GH_BACKUP=1` + `GH_TOKEN`) ; restauration automatique si disque neuf.
- PostgreSQL : tout le SQL vit dans `db.py`/`repository.py` avec placeholders
  `%s` traduits — le portage consiste à remplacer `_connect()`/`_ph()` et les
  types DDL (prévu ÉTAPE 2B+, aucune réécriture métier).

## 6. Évaluation & métriques (§12/§13)

- Règlement par famille : 1N2 multi-classes (Brier Σ(pᵢ−oᵢ)², LogLoss −ln pᵣₑₐₗ,
  distribution complète conservée) ; DC/O-U/BTTS binaires.
- Match annulé/reporté → prédictions `VOID`, exclues ; match sans gel pré-match
  → `NOT_EVALUABLE`, exclu de TOUT score.
- Métriques publiques (`/api/predictions/performance`) : accuracy, Brier,
  LogLoss, ROI/Yield/drawdown (uniquement sur cotes réelles capturées),
  par marché / compétition / version de modèle — **masquées tant que
  < 20 matchs gelés réglés** (« Évaluation en reconstruction »).
- `LEGACY` : `data/accuracy.json` (ancien système contaminé) n'est **pas**
  importé — préférer un vrai « rien » à une fausse précision (§21).

## 7. Limites assumées de cette étape (traitées aux étapes suivantes)

- Probabilités **brutes, non calibrées** (§28) — calibration en ÉTAPE 2C
  (le « 98 % » sera traité là : Dixon-Coles, Platt, plafonds).
- Source unique ESPN (multi-sources ÉTAPE 2B avec registre de provenance).
- Pas encore : xG, handicaps asiatiques, multi-bookmakers, backtest public.
