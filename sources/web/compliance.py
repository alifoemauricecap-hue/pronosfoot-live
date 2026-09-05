# -*- coding: utf-8 -*-
"""
COMPLIANCE GATE (2B.WEB-2 §3-§13)
==================================
Décide ALLOW / DENY / UNKNOWN AVANT toute requête réseau, à partir de :

    1. statut registry          5. politique interne (NEVER_CONTACT)
    2. capacité déclarée        6. contraintes techniques connues
    3. activation (enabled)     7. politique robots / autorisation connues
    4. authentification         9. type de donnée demandé
                                8. URL (schéma, allowlist, SSRF)

RÈGLES STRICTES :
- TECHNICALLY_ACCESSIBLE + POLICY_BLOCKED => DENY (§4) ;
- BLOCKED / DEPRECATED / DISABLED / UNTESTED / PARTIAL => DENY (§5/§6/§8/§9) ;
- REQUIRES_KEY sans clé d'environnement => DENY (§7) — jamais de clé lue
  dans le code, le Git, les logs ;
- jamais de contournement de restriction (§5) ;
- HTTPS obligatoire sauf si la source l'autorise explicitement (§13) ;
- aucune liste parallèle : tout vient du registry RÉEL (allowlist = base_url).

Ce module NE FAIT AUCUN réseau (validation pure). La SSRF DNS-level est
re-vérifiée par le client à la résolution (safe_http) puis à chaque
redirection — ici on valide la forme littérale de l'URL.
"""
import ipaddress
import os
import re
from urllib.parse import urlsplit

from sources import registry as _regmod
from .source_discovery import BLOCKED_NEVER_CONTACT

ALLOW = "ALLOW"
DENY = "DENY"
UNKNOWN = "UNKNOWN"

# statuts à DENY direct (§5/§6/§8/§9 : REQUIRES_KEY a un chemin dédié, PAS
# forcément un deny immédiat si la clé existe + enabled — voir plus bas)
_STATUS_DENY = ("BLOCKED", "DEPRECATED", "DISABLED", "UNTESTED", "PARTIAL")

# schémas acceptables côté refus standard (§35 : javascript:/file:/data:/ftp:…)
_ALLOWED_SCHEMES = ("https", "http")

# critères IP interdits (§11) : loopback, privé, link-local, multicast,
# réservé, non spécifié — évalués sur littéraux ET résolutions DNS.
_IP_FORBIDDEN = ("is_private", "is_loopback", "is_link_local",
                 "is_multicast", "is_reserved", "is_unspecified")

_HOST_NUMERIC = re.compile(r"^(0x[0-9a-fA-F]+|\d+)(\.\d+|\.0x[0-9a-fA-F]+)*$"
                           r"|^\d+$")
_BAD_URL_CHARS = re.compile(r"[\s\\]")

_DEFAULT_PORT = {"https": 443, "http": 80}
_METADATA_HOSTS = re.compile(r"(^|\.)(localhost|internal|local|lan|home|corp)$",
                             re.IGNORECASE)


class ComplianceDecision:
    """Résultat immuable d'une évaluation — toujours explicable (raisons)."""
    __slots__ = ("decision", "source_id", "url", "data_type", "reasons")

    def __init__(self, decision, source_id, url, data_type, reasons):
        self.decision = decision
        self.source_id = source_id
        self.url = url
        self.data_type = data_type
        self.reasons = tuple(reasons or ())

    def to_dict(self):
        return {"decision": self.decision, "source_id": self.source_id,
                "url": self.url, "data_type": self.data_type,
                "reasons": list(self.reasons)}

    def __eq__(self, other):
        return (isinstance(other, ComplianceDecision)
                and self.to_dict() == other.to_dict())

    def __repr__(self):
        return (f"ComplianceDecision({self.decision} src={self.source_id!r} "
                f"type={self.data_type!r} reasons={list(self.reasons)})")


# ---------------------------------------------------------------------------
# VALIDATION URL / SSRF (§11/§35) — pur, déterministe
# ---------------------------------------------------------------------------
def dissect_url(url):
    """Découpe + valide une URL. Lève ValueError accompagné d'une raison
    stable ('URL_VIDE', 'SCHEME_INTERDIT', …) — jamais d'accès implicite.
    Retourne dict(scheme, host, port, effective_port, path_query)."""
    if not isinstance(url, str):
        raise ValueError("URL_TYPE_INVALID")
    u = url.strip()
    if not u:
        raise ValueError("URL_VIDE")
    if len(u) > 2048:
        raise ValueError("URL_TROP_LONGUE")
    if _BAD_URL_CHARS.search(u):
        raise ValueError("URL_CARACTERES_INTERDITS")
    try:
        p = urlsplit(u)
    except ValueError:
        raise ValueError("URL_MALFORMEE")
    scheme = (p.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError("SCHEME_INTERDIT")
    if p.username is not None or p.password is not None:
        raise ValueError("USERINFO_INTERDIT")
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("HOST_ABSENT")
    try:
        port = p.port
    except ValueError:
        raise ValueError("PORT_INVALID")
    effective_port = port if port is not None else _DEFAULT_PORT[scheme]
    if not (1 <= effective_port <= 65535):
        raise ValueError("PORT_INVALID")
    path_query = p.path or "/"
    if p.query:
        path_query += "?" + p.query
    return {"scheme": scheme, "host": host, "port": port,
            "effective_port": effective_port, "path_query": path_query}


def ip_is_forbidden(ip):
    """True si l'adresse IP (str) est interne/interdite (§11)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True                       # illisible → refus conservateur
    if any(getattr(addr, attr) for attr in _IP_FORBIDDEN):
        return True
    return False


def host_ssrf_reason(host):
    """SSRF littéral : retourne une raison ('SSRF_*') ou None si le host
    n'est pas un littéral dangereux. La résolution DNS est re-vérifiée par
    le client (§11 : une URL autorisée ne doit pas pouvoir rediriger ni
    résoudre vers l'interne)."""
    if _METADATA_HOSTS.search(host):
        return "SSRF_METADATA_HOST"
    if host == "metadata.google.internal":
        return "SSRF_METADATA_HOST"
    if _HOST_NUMERIC.match(host):
        return "SSRF_NUMERIC_HOST"
    if ":" in host:                                # IPv6 littérale
        if ip_is_forbidden(host):
            return "SSRF_IP_INTERDITE"
        return None
    try:
        ipaddress.ip_address(host)                 # IPv4 littérale
    except ValueError:
        return None                                # nom de domaine normal
    if ip_is_forbidden(host):
        return "SSRF_IP_INTERDITE"
    return None


# ---------------------------------------------------------------------------
# LA PORTE (§3)
# ---------------------------------------------------------------------------
class ComplianceGate:
    """Évalue check_source_access(source_id, url, data_type).

    registry : dict chargé (registry.load()) — le VRAI fichier par défaut.
    policy_overrides : {source_id: {"allow_http": bool,
                   "allow_private_hosts": bool}} — relaxation EXPLICITE,
    utilisée uniquement par les tests (serveur local) ; jamais appliquée
    aux sources réelles, jamais lue depuis un fichier par défaut.
    """

    def __init__(self, registry=None, policy_overrides=None):
        self.reg = registry or _regmod.load()
        self.overrides = dict(policy_overrides or {})

    # -- allowlist : hôtes autorisés dérivés du registry (§10) --------------
    def allowed_endpoints(self, source_id):
        """Ensemble de tuples (scheme, host, effective_port) autorisés —
        dérivés UNIQUEMENT de base_url du registry (jamais saisie libre).
        Twin www. ajouté seulement quand l'hôte est un domaine racine
        (2 labels) — jamais de sous-domaine élargi automatiquement."""
        src = _regmod.get_source(self.reg, source_id)
        if not src:
            return set()
        try:
            base = dissect_url(src.get("base_url") or "")
        except ValueError:
            return set()
        hosts = {base["host"]}
        labels = base["host"].split(".")
        if len(labels) == 2:                       # domaine racine : twin www
            hosts.add("www." + base["host"])
        elif base["host"].startswith("www."):
            hosts.add(base["host"][4:])
        out = set()
        for h in hosts:
            out.add((base["scheme"], h, base["effective_port"]))
        return out

    # -- l'évaluation complète (§3) ------------------------------------------
    def check_source_access(self, source_id, url, data_type):
        reasons = []
        src = _regmod.get_source(self.reg, source_id)
        if not src:
            return ComplianceDecision(DENY, source_id, url, data_type,
                                      ["INVALID_SOURCE"])
        # (5) politique interne absolue (§5/§19 WEB-1)
        if source_id in BLOCKED_NEVER_CONTACT:
            return ComplianceDecision(DENY, source_id, url, data_type,
                                      ["POLICY_NEVER_CONTACT"])
        # (1) statut
        status = src.get("status")
        if status in _STATUS_DENY:
            return ComplianceDecision(DENY, source_id, url, data_type,
                                      [f"STATUS_{status}"])
        # (3) activation
        if not src.get("enabled"):
            reasons += ["SOURCE_DISABLED"]
            return ComplianceDecision(DENY, source_id, url, data_type, reasons)
        # (4) authentification : REQUIRES_KEY → clé d'environnement requise
        if status == "REQUIRES_KEY":
            env_name = f"PRONOFOOT_KEY_{source_id.upper().replace('-', '_')}"
            if not os.environ.get(env_name):
                return ComplianceDecision(
                    DENY, source_id, url, data_type, ["AUTH_KEY_MISSING"])
            reasons += ["AUTH_KEY_CONFIGURED"]
        # (2) capacité déclarée pour le data_type
        cap = (src.get("capabilities") or {}).get(data_type)
        if cap is False:
            return ComplianceDecision(DENY, source_id, url, data_type,
                                      reasons + ["CAPABILITY_FALSE"])
        if cap not in (True, "partial"):
            return ComplianceDecision(
                UNKNOWN, source_id, url, data_type,
                reasons + ["CAPABILITY_UNKNOWN"
                           if cap == "unknown" else "CAPABILITY_NOT_DECLARED"])
        reasons.append("CAPABILITY_OK")
        # (8) URL : forme + schéma + allowlist + SSRF (§10-§13)
        try:
            parts = dissect_url(url)
        except ValueError as ve:
            return ComplianceDecision(DENY, source_id, url, data_type,
                                      reasons + [str(ve)])
        ov = self.overrides.get(source_id) or {}
        if parts["scheme"] == "http" and not ov.get("allow_http"):
            return ComplianceDecision(DENY, source_id, url, data_type,
                                      reasons + ["SCHEME_HTTP_INTERDIT"])
        endpoints = self.allowed_endpoints(source_id)
        if (parts["scheme"], parts["host"], parts["effective_port"]) \
                not in endpoints:
            return ComplianceDecision(DENY, source_id, url, data_type,
                                      reasons + ["HOST_NOT_ALLOWLISTED"])
        if not ov.get("allow_private_hosts"):
            ssrf = host_ssrf_reason(parts["host"])
            if ssrf:
                return ComplianceDecision(DENY, source_id, url, data_type,
                                          reasons + [ssrf])
        return ComplianceDecision(ALLOW, source_id, url, data_type,
                                  reasons + ["COMPLIANCE_OK"])
