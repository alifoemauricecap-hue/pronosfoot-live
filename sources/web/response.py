# -*- coding: utf-8 -*-
"""
RESPONSE — objet de réponse normalisé + erreurs explicites (2B.WEB-2 §28/§29)
============================================================================
AUCUN réseau ici : ce module ne contient que des structures de données et
des erreurs. Les erreurs sont DISTINGUABLES (une classe par situation) —
jamais un « request failed » générique (§29).
"""

# ---------------------------------------------------------------------------
# ERREURS (§29) — hiérarchie explicite, .code stable pour les journaux
# ---------------------------------------------------------------------------
class SafeHttpError(Exception):
    """Base de toutes les erreurs du client HTTP sécurisé."""
    code = "SAFE_HTTP_ERROR"

    def __init__(self, message="", detail=None):
        super().__init__(message)
        self.detail = detail

    def __repr__(self):
        return f"{type(self).__name__}({self.args[0] if self.args else ''!r})"


class ComplianceDeniedError(SafeHttpError):
    """COMPLIANCE GATE : requête refusée AVANT tout réseau (§3-§8)."""
    code = "COMPLIANCE_DENIED"


class InvalidSourceError(ComplianceDeniedError):
    """source_id inconnu du registry."""
    code = "INVALID_SOURCE"


class InvalidURLError(SafeHttpError):
    """URL vide, malformée, schéma interdit, userinfo, port inhabituel (§35)."""
    code = "INVALID_URL"


class SSRFBlockedError(ComplianceDeniedError):
    """Destination réseau interne/privée (littérale ou résolue par DNS) (§11)."""
    code = "SSRF_BLOCKED"


class RedirectBlockedError(SafeHttpError):
    """Redirection excessive (>3) ou vers destination non autorisée (§12)."""
    code = "REDIRECT_BLOCKED"


class HttpTimeoutError(SafeHttpError):
    """Délai de connexion/lecture dépassé (§18)."""
    code = "TIMEOUT"


class ResponseTooLargeError(SafeHttpError):
    """Content-Length > 5 Mo ou flux réellement lu > 5 Mo (§15/§17)."""
    code = "RESPONSE_TOO_LARGE"


class InvalidContentTypeError(SafeHttpError):
    """Content-Type absent ou non autorisé (§16)."""
    code = "INVALID_CONTENT_TYPE"


class RateLimitExceededError(SafeHttpError):
    """Budget de requêtes du cycle dépassé (§20) — AUCUNE requête envoyée."""
    code = "RATE_LIMIT_EXCEEDED"


class CircuitOpenError(SafeHttpError):
    """Source suspendue par le circuit breaker (§22) — AUCUNE requête envoyée."""
    code = "CIRCUIT_OPEN"


class NetworkError(SafeHttpError):
    """Erreur de transport (DNS, connexion refusée, réinitialisation…)."""
    code = "NETWORK_ERROR"


class HttpStatusError(SafeHttpError):
    """Réponse HTTP d'erreur (4xx/5xx) après retries épuisés (§19)."""
    code = "HTTP_ERROR"

    def __init__(self, message="", status_code=None, detail=None):
        super().__init__(message, detail=detail)
        self.status_code = status_code


class InvalidMethodError(SafeHttpError):
    """Méthode autre que GET (§14) — refusée AVANT tout réseau."""
    code = "INVALID_METHOD"


# ---------------------------------------------------------------------------
# RESPONSE OBJECT (§28) — structure interne normalisée
# ---------------------------------------------------------------------------
class SafeHttpResponse:
    """Résultat d'une requête autorisée et aboutie (ou refusée : alors
    `ok=False`, body JAMAIS renvoyé sur refus — §28).

    Champs traçabilité (§47) : source_id, retrieved_at, url (finale après
    redirections), data_type. Ce module délibérément N'EXPOSE PAS
    d'« effective_at » : Safe HTTP ne décide JAMAIS de l'utilisabilité
    temporelle d'une donnée (§48) — cette décision reste à
    sources/provenance.py."""

    __slots__ = ("ok", "status_code", "url", "source_id", "data_type",
                 "content_type", "content_length", "retrieved_at",
                 "latency_ms", "body", "error", "compliance_decision",
                 "cache_hit", "attempts")

    def __init__(self, ok, url=None, source_id=None, data_type=None,
                 status_code=None, content_type=None, content_length=None,
                 retrieved_at=None, latency_ms=None, body=None, error=None,
                 compliance_decision=None, cache_hit=False, attempts=1):
        self.ok = bool(ok)
        self.status_code = status_code
        self.url = url
        self.source_id = source_id
        self.data_type = data_type
        self.content_type = content_type
        self.content_length = content_length
        self.retrieved_at = retrieved_at
        self.latency_ms = latency_ms
        self.body = body
        self.error = error
        self.compliance_decision = compliance_decision
        self.cache_hit = bool(cache_hit)
        self.attempts = attempts

    def to_dict(self):
        return {
            "ok": self.ok,
            "status_code": self.status_code,
            "url": self.url,
            "source_id": self.source_id,
            "data_type": self.data_type,
            "content_type": self.content_type,
            "content_length": self.content_length,
            "retrieved_at": self.retrieved_at,
            "latency_ms": self.latency_ms,
            "body_len": (len(self.body) if isinstance(self.body, (bytes, bytearray)) else None),
            "error": (getattr(self.error, "code", None) or
                      (type(self.error).__name__ if self.error else None)),
            "compliance_decision": (self.compliance_decision.to_dict()
                                    if hasattr(self.compliance_decision, "to_dict")
                                    else self.compliance_decision),
            "cache_hit": self.cache_hit,
            "attempts": self.attempts,
        }

    def __repr__(self):
        return (f"SafeHttpResponse(ok={self.ok} status={self.status_code} "
                f"source={self.source_id!r} type={self.data_type!r} "
                f"cache_hit={self.cache_hit})")
