# -*- coding: utf-8 -*-
"""
PACKAGE sources.web — 2B.WEB-1 → 2B.WEB-3
==========================================
- WEB-1 : Match Identity Engine + Source Discovery (hors-ligne)
- WEB-2 : Safe HTTP + Compliance Gate (couche réseau unique et gardée)
- WEB-3 : Ingestion réelle multi-sources (adapters/extractors, provenance,
  validation, conflits, fraîcheur, point-in-time store, orchestrateur,
  snapshot builder — additif, aucun code 2A/2B.1/WEB-0/WEB-1/WEB-2 modifié)

Invariants : UNKNOWN ≠ FALSE · aucune donnée inventée · aucun réseau hors
SafeHttpClient · aucune mutation du passé (append-only / insert-only) ·
ambiguïté => UNKNOWN.
"""
from . import decision, identity, source_discovery  # noqa: F401  (WEB-1)
from . import compliance, safe_http, response       # noqa: F401  (WEB-2)

__version__ = "2B.WEB-3"
__all__ = ["decision", "identity", "source_discovery",
           "compliance", "safe_http", "response"]
