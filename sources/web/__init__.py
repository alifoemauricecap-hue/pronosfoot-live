# -*- coding: utf-8 -*-
"""
PACKAGE sources.web — ÉTAPE 2B.WEB-1 (MATCH IDENTITY ENGINE + SOURCE DISCOVERY)
===============================================================================
Portée STRICTE :
- identité de matchs hors-ligne (aucune requête réseau, jamais) ;
- découverte de sources pilotée UNIQUEMENT par sources/registry.json (2B.1) ;
- aucune collecte Internet (les adapters/collecteurs ne sont PAS implémentés) ;
- aucun import du socle 2A (db/app/engine/repository/prediction_service) ;
- aucune écriture en base de données — fonctions pures et déterministes.

Modules :
- decision  : coefficients centralisés, seuils, règles de statut (§9/§14/§15)
- identity  : normalisation, alias (via sources/entity_map.json), MatchIdentity
- source_discovery : discover_sources(data_type) depuis le registry réel

Invariants : UNKNOWN ≠ FALSE · jamais d'identifiant inventé · une contradiction
majeure n'est jamais annulée par un score élevé · ambiguïté => UNKNOWN.
"""
from . import decision, identity, source_discovery  # noqa: F401

__version__ = "2B.WEB-1"
__all__ = ["decision", "identity", "source_discovery"]
