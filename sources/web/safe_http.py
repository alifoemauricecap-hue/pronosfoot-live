# -*- coding: utf-8 -*-
"""
SAFE HTTP CLIENT (2B.WEB-2)
============================
Client HTTP/1.1 minimal, bibliothèque standard UNIQUEMENT (socket + ssl),
pensé pour le futur Web Research Engine :

    COMPLIANCE GATE (compliance.py) → résolution DNS vérifiée SSRF →
    TLS vérifié (jamais désactivé) → GET uniquement → redirects ≤3 re-validés
    → Content-Type validé → corps plafonné 5 Mo (compressé ET décompressé)
    → timeouts obligatoires → retry ≤1 (502/503/504) → rate limit →
    circuit breaker → cache TTL registry → journal structuré sans secrets.

Garanties :
- AUCUNE requête ne part si la compliance n'est pas ALLOW (§3) ;
- AUCUN silent fallback : un échec ne produit JAMAIS d'appel à une autre
  source (§30) — le choix d'un replis revient à la couche policy ;
- AUCUNE donnée considérée « utilisable » : retrieved_at est un CONSTAT,
  la règle temporelle reste celle de provenance.py (§48) ;
- pas de cookies, pas de credentials, pas de secrets dans les logs (§25-§27).
"""
import gzip
import io
import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sources import registry as _regmod
from . import compliance as _comp
from .response import (
    SafeHttpResponse, SafeHttpError, ComplianceDeniedError, InvalidSourceError,
    InvalidURLError, SSRFBlockedError, RedirectBlockedError, HttpTimeoutError,
    ResponseTooLargeError, InvalidContentTypeError, RateLimitExceededError,
    CircuitOpenError, NetworkError, HttpStatusError, InvalidMethodError)

UTC = timezone.utc

# ---------------------------------------------------------------------------
# CONFIGURATION CENTRALISÉE (§15/§18-§24) — aucune valeur codée ailleurs
# ---------------------------------------------------------------------------
SAFE_HTTP_CONFIG = {
    "timeouts": {"connect_sec": 5.0, "read_sec": 12.0, "cycle_budget_sec": 25.0},
    "max_redirects": 3,
    "max_body_bytes": 5 * 1024 * 1024,          # 5 Mo lus (compressés)
    "max_decompressed_bytes": 5 * 1024 * 1024,  # anti zip-bomb (§17)
    "allowed_methods": ("GET",),                # §14 : jamais POST/PUT/…
    "content_types_allowed": ("application/json", "text/json", "text/csv",
                              "text/plain", "text/html"),
    "retry": {"max": 1, "statuses": (502, 503, 504), "delay_sec": 2.0},
    "rate_limit": {"per_source_min_interval_sec": 1.0,
                   "cycle_max_requests": 20, "cycle_window_sec": 25.0},
    "concurrency": {"max_simultaneous": 3},
    "circuit_breaker": {"failures_to_open": 5, "open_duration_sec": 1800.0},
    "cache": {"enabled": True},
    "user_agent": "PronoFoot/3.0 (+pronosfoot-live;safe-http;contact:github)",
}
_RETRYABLE = SAFE_HTTP_CONFIG["retry"]["statuses"]


def _now_iso():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# RATE LIMITER (§20) — délai min/source + budget par cycle, thread-safe
# ---------------------------------------------------------------------------
class _RateLimiter:
    def __init__(self, config, clock=time.monotonic):
        rl = config["rate_limit"]
        self.min_interval = float(rl["per_source_min_interval_sec"])
        self.cycle_max = int(rl["cycle_max_requests"])
        self.cycle_window = float(rl["cycle_window_sec"])
        self.clock = clock
        self._lock = threading.Lock()
        self._last_per_source = {}        # source_id -> monotonic du dernier appel
        self._cycle_start = self.clock()
        self._cycle_count = 0

    def acquire(self, source_id, sleep=time.sleep):
        with self._lock:
            now = self.clock()
            if now - self._cycle_start >= self.cycle_window:
                self._cycle_start = now
                self._cycle_count = 0
            if self._cycle_count >= self.cycle_max:
                raise RateLimitExceededError(
                    f"budget cycle dépassé ({self.cycle_max} requêtes)")
            last = self._last_per_source.get(source_id)
            wait = None
            if last is not None:
                remain = self.min_interval - (now - last)
                if remain > 0:
                    wait = remain
            self._cycle_count += 1
            self._last_per_source[source_id] = (now + (wait or 0.0))
        if wait:
            sleep(wait)                   # hors verrou : pas de blocage global


# ---------------------------------------------------------------------------
# CIRCUIT BREAKER (§22) — N échecs → source suspendue → demi-essai → reprise
# ---------------------------------------------------------------------------
class _CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, config, clock=time.monotonic):
        self.failures_to_open = int(config["circuit_breaker"]["failures_to_open"])
        self.open_duration = float(config["circuit_breaker"]["open_duration_sec"])
        self.clock = clock
        self._lock = threading.Lock()
        self._state = {}                  # source_id -> dict(state, fails, opened_at)

    def _get(self, source_id):
        return self._state.setdefault(
            source_id, {"state": self.CLOSED, "fails": 0, "opened_at": None})

    def check(self, source_id):
        with self._lock:
            st = self._get(source_id)
            if st["state"] == self.OPEN:
                if self.clock() - st["opened_at"] >= self.open_duration:
                    st["state"] = self.HALF_OPEN
                    return                  # un essai de récupération autorisé
                raise CircuitOpenError(
                    f"source '{source_id}' suspendue (circuit ouvert)")

    def record_success(self, source_id):
        with self._lock:
            st = self._get(source_id)
            st.update(state=self.CLOSED, fails=0, opened_at=None)

    def record_failure(self, source_id):
        with self._lock:
            st = self._get(source_id)
            if st["state"] == self.HALF_OPEN:
                st.update(state=self.OPEN, opened_at=self.clock(), fails=1)
                return
            st["fails"] += 1
            if st["fails"] >= self.failures_to_open:
                st.update(state=self.OPEN, opened_at=self.clock())

    def state(self, source_id):
        with self._lock:
            return dict(self._get(source_id))


# ---------------------------------------------------------------------------
# CACHE MÉMOIRE (§23) — abstraction : JAMAIS source de vérité, TTL registry
# ---------------------------------------------------------------------------
class _MemoryCache:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._store = {}                  # key -> (expi|None, entry)

    @staticmethod
    def key(source_id, url, data_type):
        return f"{source_id}|{data_type}|{url}"

    def get(self, key):
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            expi, entry = item
            if expi is not None and self.clock() >= expi:
                del self._store[key]
                return None
            return entry

    def set(self, key, entry, ttl_sec):
        expi = None if ttl_sec is None else self.clock() + float(ttl_sec)
        with self._lock:
            self._store[key] = (expi, entry)

    def __len__(self):
        with self._lock:
            return len(self._store)


# ---------------------------------------------------------------------------
# LE CLIENT (§11-§30)
# ---------------------------------------------------------------------------
class SafeHttpClient:
    """Un SEUL point d'entrée HTTP sécurisé pour le Web Research Engine.

    Paramètres injectables : registry (défaut = fichier réel), gate de
    compliance, config (fusionnée sur SAFE_HTTP_CONFIG), clock (tests),
    logger(callable) pour la journalisation structurée (§27), et — WEB-4
    ADDITIF — `cache` : backend de cache interchangeable (défaut = mémoire,
    interface get/set/__len__/key — ex. WebCacheSQLite persistant).
    """

    def __init__(self, registry=None, gate=None, config=None, clock=None,
                 logger=None, cache=None):
        self.reg = registry or _regmod.load()
        self.gate = gate or _comp.ComplianceGate(self.reg)
        self.config = dict(SAFE_HTTP_CONFIG)
        for k, v in (config or {}).items():
            if isinstance(v, dict) and isinstance(self.config.get(k), dict):
                self.config[k] = {**self.config[k], **v}
            else:
                self.config[k] = v
        self.clock = clock or time.monotonic
        self.logger = logger
        self._rl = _RateLimiter(self.config, clock=self.clock)
        self._cb = _CircuitBreaker(self.config, clock=self.clock)
        # WEB-4 : backend de cache interchangeable (défaut = mémoire §23) ;
        # l'interface imposée est celle de _MemoryCache (get/set/__len__/key).
        self._cache = cache if cache is not None else _MemoryCache(clock=self.clock)
        self._log_lock = threading.Lock()

    # -- journal structuré (§27) : jamais Authorization/cookies/token -------
    def _log(self, **kw):
        entry = {"ts": _now_iso(), **kw}
        for forbidden in ("authorization", "cookie", "token", "api_key",
                          "password"):
            entry.pop(forbidden, None)
        with self._log_lock:
            if callable(self.logger):
                try:
                    self.logger(entry)
                except Exception:
                    pass
        return entry

    # -- résolution DNS vérifiée (§11) ---------------------------------------
    def _resolve_checked(self, host, port, source_id):
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as ge:
            raise NetworkError(f"DNS irresoluble : {host}") from ge
        ips = sorted({ai[4][0] for ai in infos})
        if not ips:
            raise NetworkError(f"DNS vide : {host}")
        ov = getattr(self.gate, "overrides", {}).get(source_id) or {}
        if not ov.get("allow_private_hosts"):
            for ip in ips:
                if _comp.ip_is_forbidden(ip):
                    raise SSRFBlockedError(
                        f"resolution de '{host}' vers adresse interdite {ip} (§11)")
        return ips[0]

    # -- connexion TLS (§13 : jamais verify=False) ---------------------------
    def _connect(self, host, ip, port, scheme, timeout):
        raw = socket.create_connection((ip, port), timeout=timeout)
        raw.settimeout(timeout)
        if scheme == "https":
            ctx = ssl.create_default_context()     # vérification INTÉGRALE
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            return ctx.wrap_socket(raw, server_hostname=host)
        return raw

    # -- lecture plafonnée (§15/§17) ------------------------------------------
    def _read_body(self, sock, headers):
        max_b = self.config["max_body_bytes"]
        cl = headers.get("content-length")
        if cl is not None:
            try:
                declared = int(cl)
            except ValueError:
                raise NetworkError("Content-Length illisible")
            if declared > max_b:
                raise ResponseTooLargeError(
                    f"Content-Length {declared} > {max_b} octets")
        chunks, total = [], 0
        encoding = (headers.get("transfer-encoding") or "").lower()
        if "chunked" in encoding:
            # lecture chunked plafonnée
            while True:
                size_line = self._readline(sock)
                try:
                    size = int(size_line.strip().split(b";")[0], 16)
                except ValueError:
                    raise NetworkError("chunk illisible")
                if size == 0:
                    # trailers éventuels jusqu'à ligne vide
                    while True:
                        if not self._readline(sock):
                            break
                    break
                if total + size > max_b:
                    raise ResponseTooLargeError(
                        f"flux chunked > {max_b} octets")
                data = self._read_exact(sock, size)
                self._read_exact(sock, 2)         # CRLF
                chunks.append(data)
                total += size
        else:
            expected = int(cl) if cl is not None else None
            while True:
                remain = (expected - total) if expected is not None else None
                want = min(65536, remain) if remain is not None else 65536
                if want == 0:
                    break
                data = sock.recv(want)
                if not data:
                    break
                chunks.append(data)
                total += len(data)
                if total > max_b:
                    raise ResponseTooLargeError(
                        f"flux reel > {max_b} octets")
        raw = b"".join(chunks)
        if (headers.get("content-encoding") or "").lower() == "gzip":
            max_d = self.config["max_decompressed_bytes"]
            out = bytearray()
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                while True:
                    part = gz.read(min(65536, max_d + 1 - len(out)))
                    if not part:
                        break
                    out += part
                    if len(out) > max_d:
                        raise ResponseTooLargeError(
                            f"decompresse > {max_d} octets (zip-bomb ?)")
            raw = bytes(out)
        return raw

    @staticmethod
    def _readline(sock, limit=8192):
        buf = bytearray()
        while True:
            ch = sock.recv(1)
            if not ch:
                raise NetworkError("connexion fermee prematurement")
            buf += ch
            if len(buf) >= 2 and buf[-2:] == b"\r\n":
                return bytes(buf[:-2])
            if len(buf) > limit:
                raise NetworkError("ligne HTTP trop longue")

    @staticmethod
    def _read_exact(sock, n):
        buf = bytearray()
        while len(buf) < n:
            data = sock.recv(n - len(buf))
            if not data:
                raise NetworkError("connexion fermee prematurement")
            buf += data
        return bytes(buf)

    # -- requête brute --------------------------------------------------------
    def _raw_request(self, source_id, url_parts, method, started_at):
        host = url_parts["host"]
        port = url_parts["effective_port"]
        scheme = url_parts["scheme"]
        tcfg = self.config["timeouts"]
        ip = self._resolve_checked(host, port, source_id)
        sock = self._connect(host, ip, port, scheme, tcfg["connect_sec"])
        try:
            sock.settimeout(tcfg["read_sec"])
            req = (f"{method} {url_parts['path_query']} HTTP/1.1\r\n"
                   f"Host: {host}\r\n"
                   f"User-Agent: {self.config['user_agent']}\r\n"
                   f"Accept: */*\r\n"
                   f"Accept-Encoding: gzip\r\n"
                   f"Connection: close\r\n"
                   f"\r\n").encode("ascii")
            sock.sendall(req)
            status_line = self._readline(sock).decode("latin-1")
            try:
                _, code_s, reason = status_line.split(" ", 2)
                status = int(code_s)
            except ValueError:
                raise NetworkError(f"status line illisible : {status_line!r}")
            headers = {}
            while True:
                line = self._readline(sock)
                if not line:
                    break
                k, _, v = line.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            location = headers.get("location")
            if status in (301, 302, 303, 307, 308) and location:
                return {"status": status, "headers": headers, "redirect": location,
                        "body": b""}
            body = self._read_body(sock, headers)
            return {"status": status, "headers": headers, "redirect": None,
                    "body": body}
        finally:
            try:
                sock.close()
            except Exception:
                pass

    # -- validation content-type (§16) ----------------------------------------
    def _check_content_type(self, headers):
        ct = (headers.get("content-type") or "").split(";")[0].strip().lower()
        allowed = tuple(c.lower() for c in self.config["content_types_allowed"])
        if not ct:
            raise InvalidContentTypeError("Content-Type absent")
        if ct not in allowed:
            raise InvalidContentTypeError(f"Content-Type refuse : {ct}")

    # -- API PRINCIPALE --------------------------------------------------------
    def fetch(self, source_id, url, data_type, method="GET",
              allow_cache=True, _depth=0):
        """Requête unique, entièrement gardée. Lève une SafeHttpError
        explicite en cas de refus/échec — jamais de body sur refus (§28).

        AUCUN silent fallback : cette méthode ne sait PAS appeler une autre
        source (§30)."""
        t0 = self.clock()
        method = (method or "").upper()
        if method not in self.config["allowed_methods"]:
            raise InvalidMethodError(f"methode refusee : {method}")

        # 1) COMPLIANCE GATE (avant toute E/S réseau)
        decision = self.gate.check_source_access(source_id, url, data_type)
        if decision.decision != _comp.ALLOW:
            self._log(source_id=source_id, data_type=data_type, method=method,
                      status="DENY", compliance=decision.decision,
                      reasons=list(decision.reasons), latency_ms=0)
            if not _regmod.get_source(self.reg, source_id):
                raise InvalidSourceError(f"source inconnue : {source_id}")
            try:
                _comp.dissect_url(url)
            except ValueError as ve:
                raise InvalidURLError(str(ve))
            if decision.decision == _comp.UNKNOWN:
                raise ComplianceDeniedError(
                    f"UNKNOWN : capacite/data non prouvee pour {source_id}",
                    detail={"reasons": list(decision.reasons)})
            ssrf = next((r for r in decision.reasons if r.startswith("SSRF")), None)
            if ssrf:
                raise SSRFBlockedError(ssrf)
            raise ComplianceDeniedError(
                f"DENY : {source_id}", detail={"reasons": list(decision.reasons)})

        # 2) circuit breaker
        self._cb.check(source_id)

        # 3) cache (abstraction — TTL vient du registry, §23)
        cache_key = _MemoryCache.key(source_id, url, data_type)
        if allow_cache and self.config["cache"]["enabled"]:
            hit = self._cache.get(cache_key)
            if hit is not None:
                out = SafeHttpResponse(
                    ok=True, url=hit["url"], source_id=source_id,
                    data_type=data_type, status_code=hit["status"],
                    content_type=hit["content_type"],
                    content_length=hit["content_length"],
                    retrieved_at=hit["retrieved_at"],
                    latency_ms=round((self.clock() - t0) * 1000, 2),
                    body=hit["body"], compliance_decision=decision,
                    cache_hit=True, attempts=0)
                self._log(source_id=source_id, data_type=data_type,
                          method=method, status="CACHE_HIT", compliance="ALLOW",
                          latency_ms=out.latency_ms)
                return out

        # 4) rate limit (bloque AVANT réseau si budget dépassé)
        self._rl.acquire(source_id)

        attempts = 0
        last_err = None
        max_retry = self.config["retry"]["max"]
        while attempts <= max_retry:
            attempts += 1
            try:
                result = self._fetch_guards(source_id, url, data_type, method,
                                            t0, decision)
            except (NetworkError, HttpTimeoutError) as ne:
                last_err = ne
                self._cb.record_failure(source_id)
                break                          # réseau : pas de retry fin ici sauf timeout → retry géré plus bas
            except HttpStatusError as he:
                last_err = he
                if he.status_code in _RETRYABLE and attempts <= max_retry:
                    time.sleep(self.config["retry"]["delay_sec"])
                    continue
                if he.status_code and he.status_code >= 500:
                    self._cb.record_failure(source_id)
                break
            except SafeHttpError:
                raise
            if isinstance(result, SafeHttpResponse):
                if result.status_code and result.status_code >= 500:
                    self._cb.record_failure(source_id)
                else:
                    self._cb.record_success(source_id)
                return result
            break
        raise last_err

    # -- une tentative complète (redirects inclus) -----------------------------
    def _fetch_guards(self, source_id, url, data_type, method, t0, decision):
        retries_info = []
        current_url = url
        redirects = 0
        while True:
            parts = _comp.dissect_url(current_url)
            cycle_deadline = t0 + self.config["timeouts"]["cycle_budget_sec"]
            if self.clock() > cycle_deadline:
                raise HttpTimeoutError("budget cycle 25s depasse")
            try:
                raw = self._raw_request(source_id, parts, method, t0)
            except (socket.timeout, TimeoutError) as te:
                raise HttpTimeoutError("timeout") from te
            except ssl.SSLError as se:
                raise NetworkError(f"TLS echoue : {se}") from se
            except (ConnectionError, OSError) as oe:
                raise NetworkError(f"transport echoue : {oe}") from oe
            status = raw["status"]
            if raw["redirect"] is not None:
                redirects += 1
                if redirects > self.config["max_redirects"]:
                    raise RedirectBlockedError(
                        f">{self.config['max_redirects']} redirections")
                from urllib.parse import urljoin
                nxt = urljoin(current_url, raw["redirect"])
                # §12 : chaque redirection re-vérifiée (https/allowlist/SSRF)
                re_decision = self.gate.check_source_access(
                    source_id, nxt, data_type)
                if re_decision.decision != _comp.ALLOW:
                    raise RedirectBlockedError(
                        f"redirection refusee : {re_decision.reasons}")
                try:
                    np = _comp.dissect_url(nxt)
                except ValueError as ve:
                    raise RedirectBlockedError(f"redirection invalide : {ve}")
                self._resolve_checked(np["host"], np["effective_port"], source_id)
                retries_info.append(current_url)
                current_url = nxt
                continue
            if status >= 400:
                raise HttpStatusError(f"HTTP {status}", status_code=status)
            self._check_content_type(raw["headers"])
            body = raw["body"]
            ct = raw["headers"].get("content-type", "").split(";")[0].strip().lower()
            resp = SafeHttpResponse(
                ok=True, url=current_url, source_id=source_id,
                data_type=data_type, status_code=status, content_type=ct,
                content_length=len(body), retrieved_at=_now_iso(),
                latency_ms=round((self.clock() - t0) * 1000, 2), body=body,
                compliance_decision=decision, attempts=len(retries_info) + 1)
            # remplissage cache si pertinent (TTL registry — data-driven)
            # clé = URL DEMANDÉE (url), pas la cible finale : deux appels
            # identiques doivent produire le même hit (déduplication §23).
            if self.config["cache"]["enabled"]:
                ttl = _regmod.ttl_for(self.reg, data_type, source_id=source_id)
                if ttl is None or ttl > 0:
                    self._cache.set(
                        _MemoryCache.key(source_id, url, data_type),
                        {"status": status, "content_type": ct,
                         "content_length": len(body), "body": body,
                         "retrieved_at": resp.retrieved_at, "url": current_url},
                        ttl)
            self._log(source_id=source_id, data_type=data_type, method=method,
                      status="OK", http_status=status,
                      latency_ms=resp.latency_ms, bytes=len(body),
                      redirects=len(retries_info), compliance="ALLOW")
            return resp

    # -- concurrence contrôlée (§21) : ≤ 3 simultanées, configurable ----------
    def fetch_many(self, requests, max_workers=None):
        """requests : iterable de dicts {source_id, url, data_type, method?}.
        Exécute en parallèle LIMITÉ (≤ concurrency.max_simultaneous).
        Retourne list[SafeHttpResponse|SafeHttpError] dans l'ordre d'entrée —
        les erreurs sont des objets, jamais masquées silencieusement (§30)."""
        limit = max_workers or self.config["concurrency"]["max_simultaneous"]
        limit = max(1, min(limit, self.config["concurrency"]["max_simultaneous"]))
        reqs = list(requests)

        def one(rq):
            try:
                return self.fetch(rq["source_id"], rq["url"], rq["data_type"],
                                  method=rq.get("method", "GET"))
            except SafeHttpError as err:
                return err
        if limit == 1 or len(reqs) <= 1:
            return [one(r) for r in reqs]
        out = [None] * len(reqs)
        with ThreadPoolExecutor(max_workers=limit) as pool:
            for i, res in enumerate(pool.map(one, reqs)):
                out[i] = res
        return out

    # introspection (tests + observabilité)
    def circuit_state(self, source_id):
        return self._cb.state(source_id)

    def cache_size(self):
        return len(self._cache)
