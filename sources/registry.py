# -*- coding: utf-8 -*-
"""
REGISTRY MULTI-SOURCES — ÉTAPE 2B.1
===================================
Chargeur + validateur + API d'interrogation du Registry déclaratif
(sources/registry.json).

GARANTIES (voir prompt 2B.1) :
- aucune requête réseau n'est faite par CE module (jamais) ;
- UNKNOWN ≠ FALSE : une capacité jamais testée n'est pas une absence ;
- priorité par type de donnée = CONFIGURABLE (registry.json), jamais codée
  en dur dans le moteur ;
- fallback DÉTERMINISTE et PROUVÉ par les tests : les sources enabled=false
  sont simplement ignorées (la donnée n'est pas déclarée fausse — §7/§8) ;
- aucun import du socle 2A (db/app/engine/repository) : zéro risque de
  régression. Ce module est PASSIF jusqu'aux ADAPTERS (2B.2).
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(HERE, "registry.json")
ENTITY_MAP_PATH = os.path.join(HERE, "entity_map.json")

STATUSES = ("TESTED", "PARTIAL", "REQUIRES_KEY", "BLOCKED",
            "DEPRECATED", "UNTESTED", "DISABLED")
CAPABILITY_VALUES = (True, False, "partial", "unknown")

SOURCE_REQUIRED = ("source_id", "name", "type", "base_url", "status", "enabled",
                   "authentication", "free", "quota", "coverage", "capabilities",
                   "priority_role", "last_tested", "reliability", "notes", "risks")


class RegistryError(ValueError):
    """Registry invalide — levée avec la liste COMPLÈTE des erreurs."""


# ---------------------------------------------------------------------------
# CHARGEMENT + VALIDATION
# ---------------------------------------------------------------------------
def load(path=REGISTRY_PATH, strict=True):
    """Charge et valide le Registry. Retourne le dict complet.
    strict=True → lève RegistryError si la structure est invalide."""
    with open(path, encoding="utf-8") as f:
        reg = json.load(f)
    if strict:
        errors = validate(reg)
        if errors:
            raise RegistryError("REGISTRY INVALIDE :\n- " + "\n- ".join(errors))
    return reg


def validate(reg):
    """Liste des erreurs structurelles (vide = valide). Aucune imputation :
    on vérifie la FORME, jamais la 'vérité' des données sources."""
    errs = []
    caps_declared = reg.get("capabilities")
    if not isinstance(caps_declared, list) or not caps_declared:
        errs.append("'capabilities' (liste de noms) manquante ou vide")
        caps_declared = []
    capset = set(caps_declared)
    statuses = set(reg.get("statuses") or [])
    if not statuses.issuperset(STATUSES):
        errs.append(f"'statuses' doit contenir au moins {list(STATUSES)}")

    seen = set()
    sources = reg.get("sources")
    if not isinstance(sources, list) or not sources:
        errs.append("'sources' manquante ou vide")
        sources = []
    for s in sources:
        sid = s.get("source_id")
        if not sid:
            errs.append("source sans 'source_id'")
            continue
        if sid in seen:
            errs.append(f"source_id dupliqué : {sid}")
        seen.add(sid)
        for k in SOURCE_REQUIRED:
            if k not in s:
                errs.append(f"{sid} : champ requis manquant '{k}'")
        if s.get("status") not in statuses if statuses else True:
            errs.append(f"{sid} : status invalide '{s.get('status')}'")
        caps = s.get("capabilities") or {}
        for ck, cv in caps.items():
            if ck not in capset:
                errs.append(f"{sid} : capacité NON déclarée globalement '{ck}'")
            if cv not in CAPABILITY_VALUES:
                errs.append(f"{sid} : valeur de capacité invalide '{ck}={cv}' "
                            f"(attendu true/false/'partial'/'unknown' — §8)")
        if not isinstance(s.get("enabled"), bool):
            errs.append(f"{sid} : 'enabled' doit être un booléen")
        # REQUIRES_KEY/DISABLED/UNTESTED/BLOCKED ne doivent pas être enabled
        if s.get("enabled") and s.get("status") in ("REQUIRES_KEY", "UNTESTED",
                                                    "BLOCKED", "DEPRECATED"):
            errs.append(f"{sid} : enabled=true interdit avec status "
                        f"'{s.get('status')}'")
        rel = s.get("reliability") or {}
        if not (isinstance(rel.get("score"), (int, float))
                and 0.0 <= rel["score"] <= 1.0):
            errs.append(f"{sid} : reliability.score ∈ [0,1] requis")

    ids = seen
    prio = reg.get("priorities") or {}
    for dtype, chain in prio.items():
        if dtype.startswith("_"):
            continue
        if dtype not in capset and dtype not in (reg.get("ttl_defaults_sec") or {}):
            errs.append(f"priority pour un type inconnu : '{dtype}'")
        if not isinstance(chain, list) or not chain:
            errs.append(f"priority '{dtype}' : liste vide")
            continue
        for sid in chain:
            if sid not in ids:
                errs.append(f"priority '{dtype}' : source inconnue '{sid}'")

    ttl = reg.get("ttl_defaults_sec") or {}
    for k, v in ttl.items():
        if k.startswith("_"):
            continue
        if v is not None and not (isinstance(v, int) and v > 0):
            errs.append(f"ttl '{k}' invalide : {v} (int > 0 ou null requis)")

    wre = reg.get("web_research_engine") or {}
    if wre.get("enabled") is not False:
        errs.append("web_research_engine.enabled doit être false (§10 — pas "
                    "d'implémentation en 2B.1)")
    for sid in wre.get("allowed_source_ids") or []:
        if sid not in ids:
            errs.append(f"web_research_engine : source inconnue '{sid}'")
    return errs


# ---------------------------------------------------------------------------
# API D'INTERROGATION (déterministe, sans réseau)
# ---------------------------------------------------------------------------
def get_source(reg, source_id):
    for s in reg["sources"]:
        if s["source_id"] == source_id:
            return s
    return None


def sources_for(reg, capability, include_partial=True, include_unknown=False):
    """Sources activées capables de fournir `capability`.
    - True  → toujours incluses ;
    - 'partial' → incluses si include_partial ;
    - 'unknown' → incluses SEULEMENT si include_unknown=True (§8 : UNKNOWN≠FALSE) ;
    - False → JAMAIS incluses ;
    - enabled=false → JAMAIS incluses.
    Ordre = ordre du registry (stable), sauf si une chain de priorité existe
    pour ce type (alors ordre = priorité)."""
    out = []
    for s in reg["sources"]:
        if not s["enabled"]:
            continue
        v = (s.get("capabilities") or {}).get(capability)
        if v is True or (v == "partial" and include_partial) \
           or (v == "unknown" and include_unknown):
            out.append(s["source_id"])
    prio = (reg.get("priorities") or {}).get(capability)
    if prio:
        rank = {sid: i for i, sid in enumerate(prio)}
        order = {sid: i for i, sid in enumerate(out)}   # figé AVANT le tri
        out.sort(key=lambda sid: (rank.get(sid, 999), order[sid]))
    return out


def fallback_chain(reg, data_type):
    """Chaîne de repli DÉTERMINISTE pour un type de donnée : la liste
    `priorities[data_type]` filtrée sur enabled=true. Jamais silencieuse :
    l'appelant reçoit la liste ordonnée et choisit EN CONNAISSANCE la
    provenance (§7)."""
    prio = (reg.get("priorities") or {}).get(data_type) or []
    enabled = {s["source_id"] for s in reg["sources"] if s["enabled"]}
    return [sid for sid in prio if sid in enabled]


def ttl_for(reg, data_type, source_id=None):
    """TTL en secondes pour un type de donnée (None = pas d'expiration).
    Surcharge par source possible via ttl_overrides_sec. Une donnée plus
    vieille que sa TTL est 'stale' — jamais injectée dans un snapshot."""
    if source_id:
        s = get_source(reg, source_id)
        if s:
            ov = (s.get("ttl_overrides_sec") or {}).get(data_type)
            if ov is not None:
                return ov
    return (reg.get("ttl_defaults_sec") or {}).get(data_type)


def unknown_is_not_false(reg, source_id, capability):
    """Contrat §8 explicité : renvoie 'unknown' pour une capacité jamais
    testée — l'appelant NE DOIT PAS la traiter comme une absence."""
    s = get_source(reg, source_id)
    return (s.get("capabilities") or {}).get(capability, "unknown") if s else "unknown"
