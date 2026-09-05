# -*- coding: utf-8 -*-
"""
PACKAGE model2c — ÉTAPE 2C : MOTEUR PROBABILISTE EXPÉRIMENTAL (SHADOW)
======================================================================
2C est construit EN PARALLÈLE de la baseline 2A (« poisson 1.0.0 »),
qui reste IMMUTABLE (engine.py / prediction_service.py non modifiés).

Garanties du package :
- ANTI-LEAKAGE P0 : availability.is_available_at(dp, T, mode) est la porte
  unique ; aucune feature, aucun modèle, aucune calibration ne lit une
  donnée postérieure à T (modes « live » strict et « backtest » documentés) ;
- données RÉELLES uniquement : OpenLigaDB, StatsBomb open-data, ESPN,
  toujours via SafeHttpClient + ComplianceGate (WEB-4) ; toute absence
  est UNKNOWN — jamais 0, jamais d'invention ;
- reproductibilité : dataset_hash / feature_hash / code_hash / model_version ;
- statut : EXPERIMENTAL. Aucune proba publique, aucun frontend, aucun
  branchement automatique à la production. 2C ne remplace jamais 2A.
"""

__version__ = "2C.0-experimental"
