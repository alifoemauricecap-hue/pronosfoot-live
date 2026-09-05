# -*- coding: utf-8 -*-
"""
PACKAGE sources.web.web4 — ÉTAPE 2B.WEB-4
==========================================
INGESTION CONTINUE + CACHE PERSISTANT + BRANCHEMENT CONTRÔLÉ AU SNAPSHOT 2A.

    MATCH/CALENDAR → SCHEDULER → ORCHESTRATOR (WEB-3) → CACHE PERSISTANT
    → PIT STORE PERSISTANT → BRIDGE (candidats T-180/T-60/T-15)
    → repository.insert_snapshot (insert-only, hashé) → FREEZE 2A

Invariants hérités, RENFORCÉS ici par la persistance :
- un snapshot gelé est IMMUTABLE — le bridge n'écrit QUE des CANDIDATS
  (source="web4-candidate"), jamais des lignes `predictions` ;
- cache ≠ snapshot : le cache peut expirer/être remplacé, les snapshots non ;
- anti-leakage : usable_at(as_of) appliqué à la lecture (§14 hérité) ;
- ambiguïté ⇒ UNKNOWN ; aucune donnée inventée ; aucun secret persisté ;
- aucun réseau hors SafeHttpClient ; le LIVE existant n'est jamais bloqué.
"""
__version__ = "2B.WEB-4"
