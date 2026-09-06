# -*- coding: utf-8 -*-
"""
IDENTITÉ INTER-SOURCES (D2C-3 / WEB-1 hérité)
==============================================
Normalisation + alias MANUELS DÉCLARÉS (OpenLigaDB ↔ StatsBomb ↔ ESPN).
Jamais de devinette : un nom non résolu = UNKNOWN (None), il n'est pas mappé.
Les alias ici ont été construits sur les noms RÉELLEMENT OBSERVÉS dans les
téléchargements de sonde (voir ETAPE-2C-AUDIT.md, section C).
"""

import re
import unicodedata


def norm_name(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Alias : clé = forme NORMALISÉE observée, valeur = clé canonique commune.
# (bâti sur noms réels OL bl1/2024 + SB matches/9/281 — jamais inventé)
ALIASES = {
    # --- Bundesliga (OL → canonique ; SB → même canonique) ---
    "fc bayern munchen": "bayern munchen",
    "bayern munich": "bayern munchen",
    "bayer 04 leverkusen": "bayer leverkusen",
    "bayer leverkusen": "bayer leverkusen",
    "borussia dortmund": "borussia dortmund",
    "borussia monchengladbach": "borussia monchengladbach",
    "rb leipzig": "rb leipzig",
    "eintracht frankfurt": "eintracht frankfurt",
    "vfl wolfsburg": "wolfsburg",
    "wolfsburg": "wolfsburg",
    "sc freiburg": "freiburg",
    "freiburg": "freiburg",
    "tsg hoffenheim": "hoffenheim",
    "hoffenheim": "hoffenheim",
    "1 fsv mainz 05": "mainz 05",
    "fsv mainz 05": "mainz 05",
    "fc augsburg": "augsburg",
    "augsburg": "augsburg",
    "vfb stuttgart": "vfb stuttgart",
    "sv werder bremen": "werder bremen",
    "werder bremen": "werder bremen",
    "vfl bochum": "bochum",
    "bochum": "bochum",
    "1 fc union berlin": "union berlin",
    "union berlin": "union berlin",
    "1 fc koln": "fc koln",
    "fc koln": "fc koln",
    "1 fc heidenheim 1846": "heidenheim",
    "fc heidenheim": "heidenheim",
    "sv darmstadt 98": "darmstadt 98",
    "darmstadt 98": "darmstadt 98",
    "fc st pauli": "st pauli",
    "holstein kiel": "holstein kiel",
    # --- Bundesliga 2 (formes observées OL au fil des saisons) ---
    "hertha bsc": "hertha bsc",
    "fc schalke 04": "schalke 04",
    "hamburger sv": "hamburger sv",
    "fortuna dusseldorf": "fortuna dusseldorf",
    "hannover 96": "hannover 96",
    "1 fc nurnberg": "nurnberg",
    "karlsruher sc": "karlsruher sc",
    "sc paderborn 07": "paderborn",
    "sv sandhausen": "sandhausen",
    "greuther furth": "greuther furth",
    "greuther fuerth": "greuther furth",
    "spvgg greuther furth": "greuther furth",
    "eintracht braunschweig": "braunschweig",
    "1 fc kaiserslautern": "kaiserslautern",
    "fortuna koln": None,  # ambiguïté historique → UNKNOWN assumé
    "vfl osnabruck": "osnabruck",
    "ssv jahn regensburg": "jahn regensburg",
    "dynamo dresden": "dynamo dresden",
    "sg dynamo dresden": "dynamo dresden",
    # --- formes ESPN RÉELLEMENT OBSERVÉES (backup prod 2026-09-06, 2C.1) ---
    "fc cologne": "fc koln",                 # ESPN anglicise Köln
    "hamburg sv": "hamburger sv",            # ESPN vs OL "Hamburger SV"
    "hertha berlin": "hertha bsc",           # ESPN vs OL "Hertha BSC"
    "mainz": "mainz 05",                     # ESPN raccourcit
    "tsv eintracht braunschweig": "braunschweig",
    "1 fc magdeburg": "magdeburg",
    "arminia bielefeld": "arminia bielefeld",
    "dsc arminia bielefeld": "arminia bielefeld",
    "sv wehen wiesbaden": "wehen wiesbaden",
    "fc ingolstadt 04": "ingolstadt",
    "wurzburger kickers": "wurzburger kickers",
    "tsv 1860 munchen": "1860 munchen",
    "1860 munchen": "1860 munchen",
    "fsv zwickau": None,
    "erzgebirge aue": "erzgebirge aue",
    "fc erzgebirge aue": "erzgebirge aue",
    "sv elversberg": "sv 07 elversberg",  # seed OL = « SV 07 Elversberg » (corrigé 2C.1)
    "ssv ulm 1846": "ulm",
    "preussen munster": "preussen munster",
    "sc preussen munster": "preussen munster",
    "fc hansa rostock": "hansa rostock",
    "hansa rostock": "hansa rostock",
    "fc energie cottbus": "energie cottbus",
    "energie cottbus": "energie cottbus",
    "vfr aalen": "aalen",
    "fc saarbrucken": "saarbrucken",
    "1 fc saarbrucken": "saarbrucken",
    "borussia monchengladbach ii": None,  # équipe réserve ≠ première
}


def canonical_team(name):
    """Nom affiché → clé canonique commune, ou None (UNKNOWN assumé)."""
    n = norm_name(name)
    if not n:
        return None
    if n in ALIASES:
        return ALIASES[n]
    return n  # nom normalisé cohérent dans toutes les sources si identique
