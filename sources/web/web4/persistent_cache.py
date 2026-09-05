# -*- coding: utf-8 -*-
"""
CACHE HTTP PERSISTANT (2B.WEB-4 §3/§4)
=======================================
Backend SQLite pour le cache de SafeHttpClient (WEB-2). MÊME INTERFACE que
`_MemoryCache` (get / set / __len__ / key) → interchangeabilité totale.

Règles :
- CE N'EST PAS UNE SOURCE DE VÉRITÉ : une entrée peut expirer ou être
  REMPLACÉE par une réponse plus récente (§4). Les snapshots 2A, eux,
  restent immuables. CACHE ≠ SNAPSHOT.
- cache HIT ⇒ données + `retrieved_at` ORIGINAL conservés (jamais ré-horodaté) ;
- cache MISS ⇒ réseau (via SafeHttpClient uniquement) ;
- TTL = source de vérité du registry (`ttl_for`), calculé en WALL-CLOCK
  (epoch) pour survivre aux redémarrages — le cache mémoire utilisait
  time.monotonic, perdu au reboot ;
- AUCUN secret : jamais d'URL en clair (url_hash), jamais de headers,
  jamais Authorization/cookies/tokens/clés (§38) — par construction.
"""
import hashlib
import json
import time

import db as _db

#: motifs interdits dans une clé/entrée de cache (défense en profondeur §38)
_FORBIDDEN_IN_KEY = ("authorization", "cookie", "token", "api_key",
                     "password", "secret", "x-auth-token")


def _utcnow():
    return _db.utcnow()


class WebCacheSQLite:
    """Cache persistant compatible avec l'interface du cache WEB-2.

    `db_module` injectable (tests : base temporaire isolée).
    """

    def __init__(self, db_module=None, clock=None):
        self.db = db_module or _db
        self.clock = clock or time.time     # wall-clock (survit au restart)

    # -- clé : identique à _MemoryCache.key (compatibilité stricte) ----------
    @staticmethod
    def key(source_id, url, data_type):
        return f"{source_id}|{data_type}|{url}"

    @staticmethod
    def _url_hash(url):
        return hashlib.sha256((url or "").encode("utf-8")).hexdigest()

    # -- lecture ----------------------------------------------------------------
    def get(self, key):
        """Retourne l'entrée {status, content_type, content_length, body,
        retrieved_at, url} ou None. Une entrée EXPIRÉE est supprimée (miss)."""
        for bad in _FORBIDDEN_IN_KEY:
            if bad in key.lower():
                return None                     # §38 : refus silencieux sûr
        row = self.db.query(
            "SELECT * FROM web_cache WHERE cache_key=%s", (key,), one=True)
        if not row:
            return None
        exp = row["expires_at"]
        if exp is not None and self.clock() >= float(exp):
            self.db.execute("DELETE FROM web_cache WHERE cache_key=%s", (key,))
            return None
        body = row["response_payload"]
        if isinstance(body, memoryview):
            body = body.tobytes()
        checksum = hashlib.sha256(body or b"").hexdigest()
        if checksum != row["checksum"]:
            # §32 corruption détectée : on supprime l'entrée, jamais de
            # donnée douteuse fournie au pipeline.
            self.db.execute("DELETE FROM web_cache WHERE cache_key=%s", (key,))
            return None
        return {"status": row["status"], "content_type": row["content_type"],
                "content_length": row["response_size"],
                "body": body, "retrieved_at": row["retrieved_at"],
                "url": None}                    # URL jamais stockée (§38)

    # -- écriture ---------------------------------------------------------------
    def set(self, key, entry, ttl_sec):
        """Insère ou REMPLACE l'entrée (§4 autorisé pour le cache uniquement).
        `retrieved_at` vient de l'entrée (l'original est conservé)."""
        for bad in _FORBIDDEN_IN_KEY:
            if bad in key.lower():
                return False
        body = entry.get("body") or b""
        try:
            json.dumps(entry.get("retrieved_at"))     # garde-fou sérialisation
        except Exception:
            return False
        now = _utcnow()
        exp = None if ttl_sec is None else self.clock() + float(ttl_sec)
        self.db.execute(
            """INSERT INTO web_cache (cache_key, source_id, url_hash, data_type,
                   response_payload, status, retrieved_at, expires_at,
                   content_type, response_size, checksum, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cache_key) DO UPDATE SET
                   source_id=excluded.source_id, url_hash=excluded.url_hash,
                   data_type=excluded.data_type,
                   response_payload=excluded.response_payload,
                   status=excluded.status, retrieved_at=excluded.retrieved_at,
                   expires_at=excluded.expires_at,
                   content_type=excluded.content_type,
                   response_size=excluded.response_size,
                   checksum=excluded.checksum, updated_at=excluded.updated_at""",
            (key, key.split("|", 1)[0] if "|" in key else "unknown",
             self._url_hash(key.split("|", 2)[-1] if "|" in key else key),
             key.split("|")[1] if key.count("|") >= 2 else None,
             bytes(body), entry.get("status"),
             entry.get("retrieved_at"), exp, entry.get("content_type"),
             len(body), hashlib.sha256(bytes(body)).hexdigest(), now, now))
        return True

    # -- gestion ------------------------------------------------------------------
    def purge_expired(self):
        n_before = self.size()
        self.db.execute("DELETE FROM web_cache WHERE expires_at IS NOT NULL "
                        "AND expires_at <= %s", (self.clock(),))
        return n_before - self.size()

    def size(self):
        row = self.db.query("SELECT COUNT(*) AS c FROM web_cache", one=True)
        return row["c"]

    def __len__(self):
        return self.size()

    # introspection (tests/diagnostic)
    def entries(self):
        return self.db.rows_to_dicts(self.db.query(
            "SELECT cache_key, source_id, data_type, status, retrieved_at, "
            "expires_at, response_size, checksum, created_at, updated_at "
            "FROM web_cache ORDER BY updated_at DESC"))


def new_entry(status, content_type, body, retrieved_at):
    """Fabrique une entrée conforme à l'interface WEB-2 (utilisée par
    safe_http._fetch_guards — la forme EXACTE attendue au retour de get())."""
    return {"status": status, "content_type": content_type,
            "content_length": len(body or b""), "body": body,
            "retrieved_at": retrieved_at, "url": None}


def _uid():
    return uuid.uuid4().hex
