# -*- coding: utf-8 -*-
"""
CONFIGURATION CENTRALE 2C (unique source de vérité des constantes).
Tout paramètre de modèle, garde-fou ou seuil se règle ICI — nulle part
ailleurs. Chaque constante est documentée ; aucune n'est cachée.
"""

CONFIG_2C = {
    # ---- identité / statut (§24/§41) ----
    "package_version": "2C.0-experimental",
    "status": "EXPERIMENTAL",

    # ---- sources autorisées (registry WEB-4) — JAMAIS d'autre ----
    "src_openligadb": "openligadb",
    "src_statsbomb": "statsbomb_open",
    "src_espn": "espn",

    # ---- périmètre données (D2C-1/D2C-2) ----
    "ol_leagues": ["bl1", "bl2"],
    "ol_seasons": list(range(2016, 2026)),          # 2016/17 … 2025/26
    "sb_xg_competition_id": 9,                       # 1. Bundesliga
    "sb_xg_season_id": 281,                          # 2023/2024
    "match_duration_hours": 2.0,                     # effective_at ≈ coup de sifflet final (hypothèse déclarée)

    # ---- fenêtres de forme (§5) ----
    "form_windows": (3, 5, 8, 10, 15),

    # ---- Elo (§6) ----
    "elo_base": 1500.0,
    "elo_new_team_base": 1450.0,        # promu/équipe nouvelle en dessous de la base (heuristique déclarée)
    "elo_home_adv": 65.0,
    "elo_k_candidates": (16.0, 24.0, 32.0),
    "elo_k_default": 24.0,
    "elo_margin_mult": True,            # multiplicateur de différence de buts (sqrt) — documenté

    # ---- forces attaque/défense (§7) ----
    "strength_half_life_days": 180.0,
    "strength_shrink_k": 5.0,           # poids du prior ligue (≈ « matchs équivalents »)
    "strength_home_weight": 0.65,       # poids stats dom/ext vs globales si échantillon suffisant
    "strength_min_loc_matches": 3,      # sous ce seuil : ratios globaux seuls
    "strength_min_loc_weight": 0.05,
    "elo_prior_matches_full": 8,        # au-delà, plus de prior Elo dans les forces (anti double comptage)

    # ---- Poisson / Dixon-Coles (§8) ----
    "max_goals_grid": 10,
    "lambda_bounds_home": (0.10, 4.5),
    "lambda_bounds_away": (0.08, 4.0),
    "dc_xi_candidates": (0.003, 0.0065, 0.010),   # décroissance temporelle DC (/jour)
    "dc_xi_default": 0.0065,
    "dc_max_iter": 400,
    "dc_min_train_matches": 200,                  # sous ce seuil : pas de fit DC (honnêteté)
    "dc_xg_weight": 0.5,                          # poids du vrai xG comme pseudo-observation (C-xG)
    "dc_xg_min_coverage": 0.30,                   # sous 30 % de couverture xG : poids forcé à 0

    # ---- calibration (§17) ----
    "platt_lr": 0.05,
    "platt_iters": 2000,
    "isotonic_min_pairs": 30,

    # ---- garde-fous probabilités extrêmes (§21) ----
    "prob_clamp": (0.01, 0.99),
    "extreme_hi": 0.90,                            # au-delà : support minimal exigé
    "extreme_lo": 0.10,
    "extreme_min_support": 50,                     # N minimal du bin de calibration pour publier >0.90

    # ---- niveaux de données (§23) ----
    "full_min_matches": 8,            # chaque équipe ≥ 8 matchs antérieurs
    "partial_min_matches": 5,
    "low_min_matches": 2,             # en dessous : INSUFFICIENT_DATA → NO_PREDICTION

    # ---- métriques / échantillonnage (§19) ----
    "minimum_sample_size": 30,        # sous 30 : métrique marquée INSUFFICIENT_SAMPLE
    "strong_claim_n": 100,            # sous 100 : aucune conclusion forte
    "ece_bins": 10,
    "bootstrap_n": 1000,
    "bootstrap_seed": 42,
    "compare_min_n": 30,

    # ---- walk-forward (§16) ----
    "wf_test_seasons": list(range(2019, 2026)),   # test = saison, train = saisons strictement antérieures
    "wf_min_train_matches": 200,

    # ---- validation croisée hyperparamètres (antisélection-sur-le-test) ----
    "hp_validation_share": 0.30,      # la plus ancienne part des plis sert à choisir k-Elo / xi DC

    # ---- chemins artifacts hors-ligne (gitignored) ----
    "offline_dir": "data/2c_offline",
}

FEATURE_VERSION = "2c-features-1.0"
CALIBRATION_VERSION = "2c-calib-1.0"
