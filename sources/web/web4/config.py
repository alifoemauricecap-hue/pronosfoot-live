# -*- coding: utf-8 -*-
"""
CONFIGURATION CENTRALE DU SCHEDULER D'INGESTION (2B.WEB-4 §7/§10/§20/§29)
==========================================================================
TOUTES les fréquences, budgets, seuils et poids vivent ici — aucune valeur
magique disséminée dans le code. Ces valeurs sont des bornes de DONNÉES :
elles ne modifient JAMAIS le modèle de prédiction (engine.py — §39).
"""

WEB4_CONFIG = {
    # -- activation -----------------------------------------------------------
    #: variable d'environnement ; "0" ⇒ scheduler désactivé (jamais fatal)
    "enabled_env": "PRONOFOOT_WEB4",

    # -- boucle ---------------------------------------------------------------
    "tick_sec": 15.0,
    "horizon_hours": 48,                # fenêtre de sélection des matchs

    # -- §36 CANARY niveau 1 : 5–10 matchs par cycle, jamais mondial ----------
    "matches_per_cycle": 5,

    # -- §10 BUDGET RÉSEAU (identique WEB-3/WEB-2, jamais augmenté) -----------
    "requests_per_match_budget": 8,     # objectif ≤ 8 requêtes/match/cycle
    "requests_per_match_hard_cap": 20,  # HARD CAP — cycle stoppé au-delà
    # Cap du cycle ALIGNÉ sur la fenêtre du client WEB-2 (20 req / 25 s) :
    # jamais au-dessus — les matchs au-delà sont marqués CYCLE_CAP et
    # repoussés honnêtement au cycle suivant (§10 : on n'augmente PAS la
    # limite pour obtenir plus de données).
    "requests_per_cycle_cap": 20,

    # -- §7/§11 PHASES (minutes avant coup d'envoi) ---------------------------
    "phases": {"t180_min": 180, "t60_min": 60, "t15_min": 15},

    # -- §7 FRÉQUENCES minimales entre deux cycles d'un même tier -------------
    "frequencies_sec": {
        "calendar": 15 * 60,            # CALENDAR : 15 min
        "T-180": 30 * 60,               # T-180 : toutes les 30 min
        "T-60": 15 * 60,                # T-60 : toutes les 15 min
        "T-15": 5 * 60,                 # T-15 : toutes les 5 min
        "live": 30,                     # LIVE : 30 s (désactivé par défaut)
        "historical": 24 * 3600,        # batch périodique
    },

    # -- §25 LIVE : le live reste au chemin ESPN existant ; l'enrichissement
    #    WEB-4 live est coupé par défaut (intégration contrôlée, jamais
    #    bloquante). L'activation nécessitera une validation explicite.
    "live_phase_enabled": False,

    # -- §13/§14 CANDIDATS DE SNAPSHOT (jamais des lignes `predictions`) ------
    "candidate_source": "web4-candidate",
    "snapshot_windows": ("T-180", "T-60", "T-15"),

    # -- §20 QUALITÉ DE LA DONNÉE (WEB-0) — PAS une probabilité de victoire ---
    "quality_weights": {
        "reliability": 0.30,
        "freshness": 0.25,
        "confirmation": 0.20,
        "temporal_validity": 0.15,
        "completeness": 0.10,
    },

    # -- §29 SEUILS D'ALERTES INTERNES ----------------------------------------
    "alerts": {
        "source_failure_warn_pct": 50.0,    # échecs source > X % → WARNING
        "identity_unknown_warn_pct": 30.0,  # identity UNKNOWN > X % → WARNING
        "requests_per_match_warn": 8,       # > 8/match → WARNING
        "requests_per_match_error": 20,     # > 20/match → ERROR (cycle stop)
    },

    # -- divers ----------------------------------------------------------------
    "journal_capacity": 5000,
    "integrity_scan_limit": 100,        # §29 : scan de hash 2A (CRITICAL)
    "stale_cycle_factor": 4,            # reprise : dernier cycle > 4×tick
    "cycle_match_timeout_sec": 90.0,    # garde-fou temps par match
}

#: tiers planifiés par le scheduler (ordre de décision)
TIERS = ("calendar", "T-180", "T-60", "T-15")


def get():
    """Retourne une COPIE de la configuration (les tests peuvent l'altérer)."""
    import copy
    return copy.deepcopy(WEB4_CONFIG)
