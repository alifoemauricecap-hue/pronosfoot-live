# -*- coding: utf-8 -*-
"""
RÉFÉRENTIEL STADES MINIMAL (2B.WEB-4 §19)
==========================================
venue_id · name · city · country · latitude · longitude · source · confidence

RÈGLES (§19) :
- priorité Wikidata puis toute source AUTORISÉE du registry ;
- coordonnées OBLIGATOIRES et SOURCÉES : un stade sans lat/lon prouvés n'est
  PAS enregistré (jamais de coordonnées « du pays » approximatives) ;
- localisation insuffisante ⇒ weather = UNKNOWN (l'OpenMeteoAdapter ne
  construit déjà AUCUNE requête sans lat/lon — double protection WEB-3).
"""
import hashlib
import json
import re
import unicodedata

import db as _db

from .. import identity as _id
from ..normalized import now_iso


def _norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def venue_id_for(name, city=None, country=None):
    raw = f"{_norm(name)}|{_norm(city)}|{_norm(country)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class VenueStore:
    def __init__(self, db_module=None):
        self.db = db_module or _db

    def add(self, name, city=None, country=None, latitude=None,
            longitude=None, source=None, confidence=None):
        """Enregistre un stade AVEC preuve géographique. Refuse sans
        coordonnées (§19 : jamais d'approximation)."""
        if not name:
            raise ValueError("name requis")
        if latitude is None or longitude is None:
            raise ValueError("VENUE_SANS_COORDONNEES_PROUVEES — refusé (§19)")
        if not source:
            raise ValueError("source requise (provenance obligatoire)")
        lat, lon = float(latitude), float(longitude)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError(f"coordonnées invalides : {lat}, {lon}")
        vid = venue_id_for(name, city, country)
        now = now_iso()
        self.db.execute(
            """INSERT INTO web_venues (venue_id, name, city, country,
                   latitude, longitude, source, confidence,
                   created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(venue_id) DO UPDATE SET
                   latitude=excluded.latitude, longitude=excluded.longitude,
                   source=excluded.source, confidence=excluded.confidence,
                   updated_at=excluded.updated_at""",
            (vid, name, city, country, lat, lon, source,
             float(confidence) if confidence is not None else None, now, now))
        return vid

    def get(self, venue_id):
        r = self.db.query("SELECT * FROM web_venues WHERE venue_id=%s",
                          (venue_id,), one=True)
        return dict(r) if r else None

    def find(self, name, city=None, country=None):
        """Recherche exacte normalisée puis sous-chaîne UNIVOQUE (jamais de
        rattachement ambigu — ambiguïté ⇒ None ⇒ weather UNKNOWN)."""
        if not name:
            return None
        # 1) clé exacte
        vid = venue_id_for(name, city, country)
        exact = self.get(vid)
        if exact:
            return exact
        # 2) nom normalisé exact
        rows = self.db.rows_to_dicts(self.db.query(
            "SELECT * FROM web_venues"))
        n = _norm(name)
        cands = [r for r in rows if _norm(r["name"]) == n]
        if len(cands) == 1:
            return cands[0]
        if not cands:
            cands = [r for r in rows
                     if n and (n in _norm(r["name"]) or _norm(r["name"]) in n)]
        # filtre ville/pays si fourni
        if city:
            cands = [r for r in cands
                     if not r.get("city") or _norm(r["city"]) == _norm(city)]
        if country:
            cands = [r for r in cands
                     if not r.get("country")
                     or _norm(r["country"]) == _norm(country)]
        if len(cands) == 1:
            return cands[0]
        return None                     # 0 ou ambigu ⇒ None (UNKNOWN)

    def coords_for(self, name, city=None, country=None):
        v = self.find(name, city=city, country=country)
        if not v:
            return None
        return v["latitude"], v["longitude"]

    def count(self):
        return self.db.query("SELECT COUNT(*) AS c FROM web_venues",
                             one=True)["c"]

    def seed_from_entity_payload(self, name, city, country, lat, lon,
                                 source, confidence=0.9):
        """Helper d'ensemencement contrôlé (ex. résultat Wikidata P17/P115 —
        jamais automatique sans ces 5 champs prouvés)."""
        return self.add(name, city=city, country=country, latitude=lat,
                        longitude=lon, source=source, confidence=confidence)


def looks_like_stadium(text):
    """Heuristique douce pour filtrer une entité « stade » ( Wikidata P115 /
    description) — utilisée seulement en support d'une preuve explicite."""
    t = (text or "").lower()
    return any(k in t for k in ("stadium", "stade", "arena", "parc", "ground"))
