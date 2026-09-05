# -*- coding: utf-8 -*-
"""
SOURCE DISCOVERY (ÉTAPE 2B.WEB-1 §17-§20)
==========================================
Répond à : « quelles sources puis-je interroger pour un TYPE DE DONNÉE ? »
en lisant UNIQUEMENT le registry 2B.1 (sources/registry.json) via
sources/registry.py. AUCUNE liste parallèle codée en dur : on interroge
`registry.sources_for()` et `registry.fallback_chain()` (déjà validées en 2B.1).

Règles de sélection (§18) — une source est SÉLECTIONNABLE seulement si :
  1. enabled == true dans le registry ; ET
  2. son status n'est pas BLOCKED / DEPRECATED / DISABLED / UNTESTED /
     REQUIRES_KEY (sauf si enabled=true — impossible en 2B.1 puisque le
     validateur l'interdit ; la règle reste écrite pour les registries futurs) ; ET
  3. pour `for_capability` : capacité déclarée True ou 'partial'.
     'unknown' (§8 UNKNOWN≠FALSE) n'est PAS sélectionnable par défaut :
     on ne prétend pas que la source fournit la donnée — mais l'appelant
     peut demander include_unknown=True pour l'exploration consciente.

AUCUN réseau · aucune source bloquée n'est contactée (§19) · déterministe.
"""
from sources import registry as _regmod

# statuts à ne JAMAIS sélectionner automatiquement (§18/§19)
FORBIDDEN_STATUSES = ("BLOCKED", "DEPRECATED", "DISABLED", "UNTESTED",
                      "REQUIRES_KEY")

# sources explicitement interdites de contact (§19) — le registry conserve
# leur statut ; cette couche les écarte AVANT toute idée de collecte.
BLOCKED_NEVER_CONTACT = ("sofascore", "flashscore", "fotmob", "whoscored")


class SourceDiscovery:
    """Lecteur de sources piloté par le registry RÉEL (pas de copie).

    registry : dict déjà chargé (registry.load()) ou None → chargé depuis
               sources/registry.json (le vrai fichier du projet).
    """

    def __init__(self, registry=None, registry_path=None):
        if registry is None:
            registry = _regmod.load(registry_path or _regmod.REGISTRY_PATH)
        self.reg = registry

    # ------------------------------------------------------------------
    def _eligible(self, source):
        """True si le statut + enabled autorisent la sélection automatique."""
        if not source.get("enabled"):
            return False
        if source.get("status") in FORBIDDEN_STATUSES:
            return False
        if source.get("source_id") in BLOCKED_NEVER_CONTACT:
            return False
        return True

    # ------------------------------------------------------------------
    def discover(self, data_type, include_unknown=False):
        """Sources sélectionnables pour un type de donnée (§17-§18).

        Ordre = priorité du registry (fallback_chain) puis sources capables
        hors-chaîne dans l'ordre du registry. Jamais silencieux : utilisez
        explain() pour connaître les sources écartées et pourquoi."""
        chain = _regmod.fallback_chain(self.reg, data_type)
        capable = _regmod.sources_for(self.reg, data_type,
                                      include_partial=True,
                                      include_unknown=include_unknown)
        out = []
        for sid in chain:
            src = _regmod.get_source(self.reg, sid)
            if src and self._eligible(src) and sid in capable:
                out.append(sid)
        for sid in capable:                    # capables hors chaîne, après
            if sid in out:
                continue
            src = _regmod.get_source(self.reg, sid)
            if src and self._eligible(src):
                out.append(sid)
        return out

    # raccourcis nommés — simple confort de lecture (§17), tous délèguent à
    # discover() : AUCUNE liste n'est codée en dur ici.
    def discover_sources(self, data_type):
        return self.discover(data_type)

    # ------------------------------------------------------------------
    def explain(self, data_type, include_unknown=False):
        """Rapport honnête : sélectionnées + écartées AVEC la raison.
        Jamais silencieux (§7 registry / §18 prompt)."""
        selected = self.discover(data_type, include_unknown=include_unknown)
        rejected = []
        for s in self.reg["sources"]:
            sid = s["source_id"]
            if sid in selected:
                continue
            reasons = []
            if not s.get("enabled"):
                reasons.append("ENABLED_FALSE")
            if s.get("status") in FORBIDDEN_STATUSES:
                reasons.append(f"STATUS_{s['status']}")
            if sid in BLOCKED_NEVER_CONTACT:
                reasons.append("NEVER_CONTACT_POLICY")
            cap = (s.get("capabilities") or {}).get(data_type)
            if cap is False:
                reasons.append("CAPABILITY_FALSE")
            elif cap == "unknown":
                reasons.append("CAPABILITY_UNKNOWN")   # UNKNOWN ≠ FALSE (§8)
            elif cap is None:
                reasons.append("CAPABILITY_NOT_DECLARED")
            rejected.append({"source_id": sid, "status": s.get("status"),
                             "enabled": s.get("enabled"),
                             "capability": cap if cap is not None else "unknown",
                             "reasons": reasons})
        return {"data_type": data_type, "selected": selected,
                "rejected": rejected,
                "registry_version": self.reg.get("registry_version")}

    # ------------------------------------------------------------------
    def unknown_capability_sources(self, data_type):
        """§8 explicité : sources dont la capacité est 'unknown' —
        listées pour information, JAMAIS sélectionnées comme vraies."""
        out = []
        for s in self.reg["sources"]:
            if (s.get("capabilities") or {}).get(data_type) == "unknown":
                out.append(s["source_id"])
        return out


# ---------------------------------------------------------------------------
def discover_sources(data_type, registry=None, include_unknown=False):
    """API fonctionnelle directe (§17) : discover_sources("xg") → [...]."""
    return SourceDiscovery(registry).discover(data_type,
                                              include_unknown=include_unknown)
