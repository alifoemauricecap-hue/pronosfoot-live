# -*- coding: utf-8 -*-
"""
DECISION — coefficients CENTRALISÉS du Match Identity Engine (ÉTAPE 2B.WEB-1)
=============================================================================
§9/§14/§15 du cahier des charges : les poids, seuils et gardes NE SONT PAS
dispersés dans le code métier. Tout est ici, regroupé dans DEFAULT_CONFIG
(consommé tel quel par identity.py — surcharge possible par injection).

AUCUN réseau · AUCUN import du socle · déterministe (ordre figé partout).
"""

# ---------------------------------------------------------------------------
# STATUTS OBLIGATOIRES (§8)
# ---------------------------------------------------------------------------
MATCH_AUTO = "MATCH_AUTO"                  # confiance ≥ seuil auto
MATCH_JOURNALIZED = "MATCH_JOURNALIZED"    # seuil journalisé ≤ confiance < auto
NO_MATCH = "NO_MATCH"                      # contradiction majeure prouvée
UNKNOWN = "UNKNOWN"                        # preuves insuffisantes / ambiguïté

STATUSES = (MATCH_AUTO, MATCH_JOURNALIZED, NO_MATCH, UNKNOWN)

# ---------------------------------------------------------------------------
# CONFIGURATION CENTRALE (tous les coefficients du moteur)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    # --- seuils de décision (§15) ----------------------------------------
    "thresholds": {
        "auto": 0.95,               # ≥ 0.95 → MATCH_AUTO
        "journalized": 0.80,        # ≥ 0.80 → MATCH_JOURNALIZED
        "multi_margin": 0.05,       # deux candidats à moins de 0.05 → ambigu
    },
    # --- poids des preuves (§9) — total sans external_id = 0.90 ----------
    # modèle à CRÉDITS : chaque preuve vérifiée ajoute son poids ; un bonus
    # n'existe que pour l'identifiant externe DE MÊME FOURNISSEUR identique.
    "weights": {
        "external_id": 0.35,        # même source + même id = preuve très forte (bonus)
        "home_team": 0.22,
        "away_team": 0.22,
        "competition": 0.13,
        "date": 0.14,
        "time": 0.09,
        "stadium": 0.10,
    },
    # --- crédit réel appliqué selon la QUALITÉ du rapprochement d'équipe --
    "team_credit": {
        "exact": 1.00,              # même identité canonique (book)
        "alias": 1.00,              # alias déclaré vers le même canonique
        "fuzzy_strong": 0.75,       # ressemblance très forte, non gardée
        "fuzzy_weak": 0.40,         # ressemblance moyenne, non gardée
    },
    # --- similarité textuelle (difflib, déterministe) ---------------------
    "fuzzy": {
        "strong": 0.96,
        "weak": 0.85,
    },
    # --- date / heure (§12) ------------------------------------------------
    "date": {
        "window_days": 1,           # fenêtre candidate ±1 j ; au-delà = contradiction
        "next_day_factor": 0.5,     # crédit date si exactement ±1 jour
    },
    "time": {
        "match_min": 25,            # |Δ| ≤ 25 min → TIME_MATCH
        "approx_min": 181,          # |Δ| ≤ 181 min → TIME_APPROX (crédit moitié)
        "approx_factor": 0.5,
    },
    # --- noms qu'on ne transforme JAMAIS seuls en équipe précise (§4/§25) --
    # après normalisation+retrait des suffixes, ces formes restent AMBIGUËS.
    "guarded_names": (
        "paris", "united", "city", "olympique", "saint germain", "sporting",
        "athletic", "atletico", "real", "dynamo", "dinamo", "rovers",
        "wanderers", "rangers", "county", "albion", "vale", "town",
        "villa", "forest", "wolves", "eagles", "stars",
    ),
    # --- suffixes football courants (retraités UNIQUEMENT pour la comparaison
    #     fuzzy de base — jamais pour déclarer une identité à eux seuls) -----
    "football_tokens": (
        "fc", "cf", "afc", "sc", "ac", "as", "rc", "rfc", "bk", "if", "sk",
        "fk", "ik", "ud", "cd", "ca", "cp", "se", "sa", "sad", "bc", "fck",
        "vfl", "vfb", "tsv", "bv", "sv", "ssc", "rcd", "ogc", "asm", "kv",
        "vv", "kd", "club", "football", "calcio", "futbol", "de",
    ),
    # --- alias de compétitions (§13) : forme normalisée → canonique --------
    #    table DÉCLARATIVE : les équivalences Neptune sont ici, nulle part ailleurs.
    "competition_aliases": {
        "premier league": "premier league",
        "english premier league": "premier league",
        "epl": "premier league",
        "eng.1": "premier league",
        "championship": "championship",
        "english championship": "championship",
        "eng.2": "championship",
        "ligue 1": "ligue 1",
        "ligue 1 uber eats": "ligue 1",
        "french ligue 1": "ligue 1",
        "france ligue 1": "ligue 1",
        "fra.1": "ligue 1",
        "ligue 2": "ligue 2",
        "fra.2": "ligue 2",
        "bundesliga": "bundesliga",
        "german bundesliga": "bundesliga",
        "ger.1": "bundesliga",
        "2. bundesliga": "2. bundesliga",
        "ger.2": "2. bundesliga",
        "la liga": "la liga",
        "laliga": "la liga",
        "primera division": "la liga",
        "spanish la liga": "la liga",
        "esp.1": "la liga",
        "serie a": "serie a",
        "italian serie a": "serie a",
        "ita.1": "serie a",
        "eredivisie": "eredivisie",
        "ned.1": "eredivisie",
        "mls": "mls",
        "major league soccer": "mls",
        "usa.1": "mls",
        "nwsl": "nwsl",
        "usa.nwsl": "nwsl",
        "liga mx": "liga mx",
        "mex.1": "liga mx",
        "brasileirao": "brasileirao",
        "bra.1": "brasileirao",
        "saudi pro league": "saudi pro league",
        "ksa.1": "saudi pro league",
        "uefa champions league": "uefa champions league",
        "champions league": "uefa champions league",
        "ucl": "uefa champions league",
        "uefa europa league": "uefa europa league",
        "europa league": "uefa europa league",
        "uel": "uefa europa league",
        "coupe de france": "coupe de france",
        "d1 feminine": "d1 feminine",
        "division 1 feminine": "d1 feminine",
        "d1 feminin": "d1 feminine",
        "arkema d1": "d1 feminine",
        "fra.f": "d1 feminine",
        "womens super league": "womens super league",
        "wsl": "womens super league",
        "eng.w": "womens super league",
    },
    # --- catégories d'équipes (§38) : hommes / femmes / jeunes -------------
    #    marqueurs détectés sur forme normalisée (tokens ENTIERS uniquement).
    "category_women_tokens": (
        "women", "woman", "ladies", "dames", "feminine", "feminin",
        "feminines", "femenil", "femenino", "femenina", "frauen", "femminile",
    ),
    "category_youth_tokens": (
        "youth", "junior", "juniors", "reserves", "reserve", "academy",
    ),
    # niveaux jeunes explicites détectés en plus (regex) : u15..u23
    "category_youth_level_regex": r"\bu(1[5-9]|2[0-3])\b",
}

# ---------------------------------------------------------------------------
# RAISONS (codes stables — consommés par le journal de décision)
# ---------------------------------------------------------------------------
# preuves positives
R_EXTERNAL_ID_MATCH = "EXTERNAL_ID_MATCH"
R_HOME_TEAM_EXACT = "HOME_TEAM_EXACT"
R_HOME_TEAM_ALIAS = "HOME_TEAM_ALIAS"
R_HOME_TEAM_FUZZY_STRONG = "HOME_TEAM_FUZZY_STRONG"
R_HOME_TEAM_FUZZY_WEAK = "HOME_TEAM_FUZZY_WEAK"
R_AWAY_TEAM_EXACT = "AWAY_TEAM_EXACT"
R_AWAY_TEAM_ALIAS = "AWAY_TEAM_ALIAS"
R_AWAY_TEAM_FUZZY_STRONG = "AWAY_TEAM_FUZZY_STRONG"
R_AWAY_TEAM_FUZZY_WEAK = "AWAY_TEAM_FUZZY_WEAK"
R_COMPETITION_MATCH = "COMPETITION_MATCH"
R_DATE_MATCH = "DATE_MATCH"
R_DATE_NEXT_DAY = "DATE_NEXT_DAY"
R_TIME_MATCH = "TIME_MATCH"
R_TIME_APPROX = "TIME_APPROX"
R_STADIUM_MATCH = "STADIUM_MATCH"
# neutres (ni crédit ni pénalité)
R_EXTERNAL_IDS_DIFFERENT_PROVIDERS = "EXTERNAL_IDS_DIFFERENT_PROVIDERS"
R_COMPETITION_UNKNOWN = "COMPETITION_UNKNOWN"
R_COMPETITION_MISSING = "COMPETITION_MISSING"
R_TIME_MISSING = "TIME_MISSING"
R_TIME_DIFFERENT = "TIME_DIFFERENT"
R_STADIUM_MISSING = "STADIUM_MISSING"
R_STADIUM_DIFFERENT = "STADIUM_DIFFERENT"
R_TEAM_GUARDED_HOME = "TEAM_GUARDED_HOME"
R_TEAM_GUARDED_AWAY = "TEAM_GUARDED_AWAY"
R_TEAM_UNMATCHED_HOME = "TEAM_UNMATCHED_HOME"
R_TEAM_UNMATCHED_AWAY = "TEAM_UNMATCHED_AWAY"
R_TEAM_AMBIGUOUS_HOME = "TEAM_AMBIGUOUS_HOME"
R_TEAM_AMBIGUOUS_AWAY = "TEAM_AMBIGUOUS_AWAY"
# contradictions majeures (veto — annulent le quota, §15)
R_EXTERNAL_ID_CONTRADICTION = "EXTERNAL_ID_CONTRADICTION"
R_TEAM_CONTRADICTION_HOME = "TEAM_CONTRADICTION_HOME"
R_TEAM_CONTRADICTION_AWAY = "TEAM_CONTRADICTION_AWAY"
R_COMPETITION_CONTRADICTION = "COMPETITION_CONTRADICTION"
R_DATE_DIFFERENT = "DATE_DIFFERENT"
R_CATEGORY_MISMATCH = "CATEGORY_MISMATCH"
# décisions structurelles
R_HOME_AWAY_INVERSION = "HOME_AWAY_INVERSION"
R_MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
R_NO_CANDIDATES = "NO_CANDIDATES"
R_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
R_INFO_AFTER_T = "INFO_AFTER_T"

CONTRADICTION_PRIORITY = (
    R_EXTERNAL_ID_CONTRADICTION,
    R_CATEGORY_MISMATCH,
    R_TEAM_CONTRADICTION_HOME,
    R_TEAM_CONTRADICTION_AWAY,
    R_COMPETITION_CONTRADICTION,
    R_DATE_DIFFERENT,
)


def decide_status(score, contradictions, config=None):
    """§15 — traduit un score agrégé en statut, SANS qu'un score élevé
    puisse annuler une contradiction majeure.

    score : float déjà plafonné ∈ [0,1] (arrondi par l'appelant)
    contradictions : iterable de codes raison « majeure » (peut être vide)
    retourne (status, decision_reason_principale).
    """
    cfg = config or DEFAULT_CONFIG
    th = cfg["thresholds"]
    contras = list(contradictions or ())
    if contras:
        dominant = next((c for c in CONTRADICTION_PRIORITY if c in contras),
                        contras[0])
        return NO_MATCH, dominant
    if score >= th["auto"]:
        return MATCH_AUTO, "CONFIDENCE_ABOVE_AUTO_THRESHOLD"
    if score >= th["journalized"]:
        return MATCH_JOURNALIZED, "CONFIDENCE_ABOVE_JOURNALIZED_THRESHOLD"
    return UNKNOWN, R_INSUFFICIENT_DATA


def pick_best_candidate(scored, config=None):
    """§16 — choix du meilleur candidat parmi des scores calculés.

    scored : liste de tuples (index, score) déjà éligibles (jamais NO_MATCH).
    Retourne (gagnant_index_ou_None, multiple: bool, ordre_complet).
    - deux meilleurs à moins de multi_margin → (None, True, ordre)
    - liste vide → (None, False, [])
    Déterministe : tri par (-score, index)."""
    cfg = config or DEFAULT_CONFIG
    margin = cfg["thresholds"]["multi_margin"]
    if not scored:
        return None, False, []
    ordered = sorted(scored, key=lambda t: (-t[1], t[0]))
    if len(ordered) == 1:
        return ordered[0][0], False, ordered
    top, second = ordered[0], ordered[1]
    if (top[1] - second[1]) < margin:
        return None, True, ordered
    return top[0], False, ordered
