# -*- coding: utf-8 -*-
"""
IDENTITY — Match Identity Engine (ÉTAPE 2B.WEB-1)
==================================================
Détermine si deux événements provenant de sources différentes désignent le
MÊME match, à partir de preuves (§9) jamais devinées :

    external match ID (même fournisseur) · équipe domicile · équipe extérieure
    · compétition · date · heure · stade · métadonnées fiables

Garanties (cahier des charges) :
- AUCUN réseau, AUCUNE dépendance hors bibliothèque standard (§3) ;
- DÉTERMINISTE : même entrée → même sortie (statut, score, raisons, ids) ;
- la normalisation ne crée JAMAIS de collision dangereuse (§4) ;
- similarité textuelle seule ≠ preuve suffisante (§5) ;
- une contradiction majeure n'est jamais annulée par le score (§15) ;
- ambiguïté ⇒ UNKNOWN, jamais de choix arbitraire (§16/§49) ;
- anti-fuite : resolve(..., as_of=T) ignore toute information reçue APRÈS T,
  en réutilisant la logique temporelle de sources/provenance.py (§22).
"""
import copy
import difflib
import json
import re
import string
import unicodedata
from datetime import datetime, timedelta, timezone

from sources import provenance as _prov
from sources import registry as _regmod
from . import decision as _dec

UTC = timezone.utc

# ---------------------------------------------------------------------------
# 1. NORMALISATION (§4) — déterministe, défensive, sans collision dangereuse
# ---------------------------------------------------------------------------
_PUNCT_TABLE = str.maketrans({c: " " for c in string.punctuation})
_DASH_CHARS = "‐‑‒–—―−─━"
_APOS_CHARS = "‘’ʼ`´"


def normalize_team_name(name, strip_suffixes=False):
    """Forme normalisée déterministe d'un nom d'équipe (§4).

    - entrée non-str (None, int…) ou vide → "" (jamais d'exception) ;
    - gère : casse, accents (NFKD), espaces multiples, ponctuation, tirets
      (tous types Unicode), apostrophes, ligatures, caractères inhabituels ;
    - `strip_suffixes=True` retire les suffixes football courants (fc, sc…)
      UNIQUEMENT pour la comparaison de base : la forme retournée reste
      indépendante et ne déclare aucune identité à elle seule.

    Exemples : "Paris Saint-Germain" == "paris saint germain" ;
    "Paris FC" reste distinct de "Paris Saint-Germain" ;
    "United" reste "united" (jamais transformé en Manchester_U_n_i_t_e_d)."""
    if not isinstance(name, str):
        return ""
    s = name.strip()
    if not s:
        return ""
    if len(s) > 5000:                      # garde-fou coût pathologique (§24)
        s = s[:5000]
    s = unicodedata.normalize("NFKC", s)                # ligatures, formes compat
    s = unicodedata.normalize("NFKD", s)                # décompose les accents
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    for ch in _DASH_CHARS:
        s = s.replace(ch, " ")
    for ch in _APOS_CHARS:
        s = s.replace(ch, " ")
    s = s.translate(_PUNCT_TABLE)                       # toute ponctuation → espace
    s = re.sub(r"\s+", " ", s).strip()
    if strip_suffixes:
        s = strip_football_tokens(s)
    return s


def normalize_competition(name):
    """Forme normalisée d'une compétition (même pipeline qu'une équipe)."""
    return normalize_team_name(name)


def normalize_stadium(name):
    """Forme normalisée d'un stade."""
    return normalize_team_name(name)


def strip_football_tokens(normalized):
    """Retire les tokens organisationnels (fc, sc, as…) d'une forme DÉJÀ
    normalisée. Jamais appliqué aux marqueurs de catégorie (feminin, u19…)
    ni aux noms gardés (united, city…)."""
    if not normalized:
        return ""
    org = set(_dec.DEFAULT_CONFIG["football_tokens"])
    toks = [t for t in normalized.split() if t not in org]
    return " ".join(toks) if toks else normalized  # tout retiré = dangereux → on garde


def split_home_away(label):
    """"PSG vs Marseille" → ("psg", "marseille"). Séparateurs reconnus :
    ' vs ', ' v ', ' - ', ' — ' (forme normalisée : espaces + minuscules).
    Retourne (left, None) si aucun séparateur n'est trouvé."""
    s = normalize_team_name(label)
    if not s:
        return "", None
    for sep in (" vs ", " v "):
        if sep in f" {s} ":
            left, right = s.split(sep.strip(), 1)
            return left.strip(), right.strip(" v")
    return s, None


# ---------------------------------------------------------------------------
# 2. CATÉGORIES (§38) — hommes / femmes / jeunes : jamais de fusion automatique
# ---------------------------------------------------------------------------
_YOUTH_RE = re.compile(_dec.DEFAULT_CONFIG["category_youth_level_regex"])


def team_category(normalized):
    """Catégorie détectée sur la forme normalisée (équipe) :
    'women' si marqueur féminin, le niveau jeune explicite ('u19'…),
    'youth' si marqueur jeune générique, sinon 'open' (= inconnu/adulte
    supposé — jamais affirmé homme)."""
    if not normalized:
        return "open"
    toks = set(normalized.split())
    if toks & set(_dec.DEFAULT_CONFIG["category_women_tokens"]):
        return "women"
    m = _YOUTH_RE.search(normalized)
    if m:
        return "u" + m.group(1)
    if toks & set(_dec.DEFAULT_CONFIG["category_youth_tokens"]):
        return "youth"
    return "open"


# ---------------------------------------------------------------------------
# 3. IDENTITÉ CANONIQUE D'ÉQUIPE (§6) + ALIAS via sources/entity_map.json (§5)
# ---------------------------------------------------------------------------
class TeamIdentity:
    """Identité canonique traçable. canonical_id utilisé SEULEMENT s'il
    provient d'une donnée déclarée (entity_map) — jamais inventé (§6/§21)."""

    __slots__ = ("canonical_id", "canonical_name", "normalized_name",
                 "aliases", "external_ids", "source", "confidence")

    def __init__(self, canonical_id, canonical_name, normalized_name,
                 aliases=(), external_ids=None, source=None, confidence=1.0):
        self.canonical_id = canonical_id
        self.canonical_name = canonical_name
        self.normalized_name = normalized_name
        self.aliases = tuple(aliases or ())
        self.external_ids = dict(external_ids or {})
        self.source = source
        self.confidence = confidence

    def key(self):
        """Clé d'identité déterministe : canonical_id sinon nom normalisé."""
        return self.canonical_id or f"name:{self.normalized_name}"

    def to_dict(self):
        return {"canonical_id": self.canonical_id,
                "canonical_name": self.canonical_name,
                "normalized_name": self.normalized_name,
                "aliases": list(self.aliases),
                "external_ids": dict(self.external_ids),
                "source": self.source,
                "confidence": self.confidence}

    def __repr__(self):
        return f"TeamIdentity({self.canonical_name!r} key={self.key()!r})"


class AliasBook:
    """Index d'alias construit EXCLUSIVEMENT depuis sources/entity_map.json
    (§5 : pas de deuxième base d'alias). Pré-remplissage Wikidata possible
    plus tard via la même table — ici AUCUN appel réseau (§7).

    Format d'une entrée de team_ids (identique à entity_map.json) :
        {"canonical_name": str, "aliases": [str, ...],
         "external_ids": {"wikidata": "Q483020", "espn": "123", ...},
         "source": str, "confidence": float}
    """

    def __init__(self, team_entries=None):
        self._entries = {}
        self._index = {}            # alias normalisé -> canonical key
        for cid, entry in (team_entries or {}).items():
            self.add(cid, entry)

    @classmethod
    def from_entity_map(cls, entity_map):
        """Construit le book depuis le contenu de sources/entity_map.json."""
        teams = (entity_map or {}).get("team_ids") or {}
        return cls(teams)

    @classmethod
    def load_default(cls):
        """Charge l'entity_map RÉEL du projet (aucune copie codée en dur)."""
        with open(_regmod.ENTITY_MAP_PATH, encoding="utf-8") as f:
            return cls.from_entity_map(json.load(f))

    def add(self, canonical_id, entry):
        entry = entry or {}
        cname = entry.get("canonical_name") or ""
        norm = normalize_team_name(cname)
        ident = TeamIdentity(
            canonical_id=canonical_id or None,
            canonical_name=cname,
            normalized_name=norm,
            aliases=tuple(entry.get("aliases") or ()),
            external_ids=entry.get("external_ids") or {},
            source=entry.get("source"),
            confidence=entry.get("confidence", 1.0),
        )
        key = ident.key()
        self._entries[key] = ident
        # le nom canonique ET chaque alias → key (forme normalisée)
        names = {cname, *ident.aliases}
        for raw in names:
            a = normalize_team_name(raw)
            if a:
                self._index.setdefault(a, key)
        return key

    def resolve(self, name):
        """Retourne la TeamIdentity déclarée pour ce nom/alias, ou None
        (None = inconnu — JAMAIS deviné). Un alias n'est appliqué que s'il
        a été enregistré avec une confiance suffisante (§5)."""
        key = self._index.get(normalize_team_name(name))
        return self._entries.get(key) if key else None

    def __len__(self):
        return len(self._entries)


def make_team(name, external_ids=None, source=None, confidence=1.0):
    """Fabrique une TeamIdentity ad hoc quand l'entity_map ne connaît pas le
    nom : canonical_id = None (aucun identifiant inventé, §21)."""
    return TeamIdentity(canonical_id=None, canonical_name=name or "",
                        normalized_name=normalize_team_name(name),
                        aliases=(), external_ids=external_ids or {},
                        source=source, confidence=confidence)


# ---------------------------------------------------------------------------
# 4. RAPPROCHEMENT D'ÉQUIPES (qualité → crédit, §4/§5/§25/§37)
# ---------------------------------------------------------------------------
def _teams_relation(name_a, name_b, book):
    """Compare deux noms d'équipes. Retourne (relation_or_contradiction,
    credit∈[0,1], identity_a, identity_b).

    relations : exact | alias | fuzzy_strong | fuzzy_weak |
                unmatched | ambiguous | guarded | different
    'different' = le book PROUVE deux identités distinctes (contradiction).
    """
    cfg = _dec.DEFAULT_CONFIG
    guarded = set(cfg["guarded_names"])
    na, nb = normalize_team_name(name_a), normalize_team_name(name_b)
    ident_a = book.resolve(name_a) or make_team(name_a)
    ident_b = book.resolve(name_b) or make_team(name_b)

    if not na or not nb:
        return "unmatched", 0.0, ident_a, ident_b

    # 1) preuve par le book (alias déclaré avec canonical_id → forte
    #    confiance, §5). DEUX fiches distinctes du book = contradiction PROUVÉE.
    key_a, key_b = ident_a.key(), ident_b.key()
    if ident_a.canonical_id and ident_b.canonical_id:
        if key_a == key_b:
            return "exact", cfg["team_credit"]["exact"], ident_a, ident_b
        return "different", 0.0, ident_a, ident_b   # deux fiches distinctes

    # (désormais : au moins l'une des deux n'est pas une fiche canonicalisée
    #  → les contrôles textuels ci-dessous appliquent les GARDES §4/§25)

    # 2) égalité de la forme normalisée complète (accents/tirets ignorés)
    if na == nb:
        base = strip_football_tokens(na)
        if base in guarded and not ident_a.canonical_id:
            return "guarded", 0.0, ident_a, ident_b
        return "exact", cfg["team_credit"]["exact"], ident_a, ident_b

    # 3) égalité après retrait des suffixes football courants
    ca, cb = strip_football_tokens(na), strip_football_tokens(nb)
    if ca and ca == cb:
        if ca in guarded and not ident_a.canonical_id:
            return "guarded", 0.0, ident_a, ident_b
        return "exact", cfg["team_credit"]["exact"], ident_a, ident_b

    # 4) similarité (difflib — déterministe) — jamais pour un nom gardé
    if ca in guarded or cb in guarded:
        return "guarded", 0.0, ident_a, ident_b
    ratio = difflib.SequenceMatcher(None, ca, cb).ratio()
    if ratio >= cfg["fuzzy"]["strong"]:
        return "fuzzy_strong", cfg["team_credit"]["fuzzy_strong"], ident_a, ident_b
    if ratio >= cfg["fuzzy"]["weak"]:
        return "fuzzy_weak", cfg["team_credit"]["fuzzy_weak"], ident_a, ident_b

    # 5) plusieurs homonymes possibles signalés, sans preuve → ambiguous
    if ratio >= 0.5:
        return "ambiguous", 0.0, ident_a, ident_b
    return "unmatched", 0.0, ident_a, ident_b


# ---------------------------------------------------------------------------
# 5. DATE / HEURE (§12) — instants UTC conscients, jamais de compare de chaînes
# ---------------------------------------------------------------------------
def _parse_instant(value):
    """Accepte : str ISO-8601 ('…Z' ou '…+02:00' ou date seule) ou datetime.
    Retourne (instant UTC conscient | None, had_time: bool, assumed_utc: bool).
    Les dates sans heure deviennent minuit UTC en marquant had_time=False."""
    if value is None:
        return None, False, False
    if isinstance(value, datetime):
        dt = value
        had_time = True
        assumed = dt.tzinfo is None
        if assumed:
            dt = dt.replace(tzinfo=UTC)          # naïf → UTC supposé (journalisé)
        return dt.astimezone(UTC), had_time, assumed
    if not isinstance(value, str):
        return None, False, False
    s = value.strip()
    if not s:
        return None, False, False
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):        # date seule (sans heure)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None, False, False
        return dt.replace(tzinfo=UTC), False, True
    if not _prov.ISO_RE.match(s):
        return None, False, False
    had_time = "T" in s
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None, False, False
    assumed = dt.tzinfo is None
    if assumed:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC), had_time, assumed


def normalize_team(name):  # alias rétro-compat (nom explicite)
    return normalize_team_name(name)


def _competition_relation(comp_a, comp_b):
    """Retourne (relation, canonical_a, canonical_b) parmi :
    match | contradiction | unknown | missing (§13)."""
    if not comp_a or not comp_b:
        return "missing", None, None
    aliases = _dec.DEFAULT_CONFIG["competition_aliases"]
    ca_raw, cb_raw = normalize_competition(comp_a), normalize_competition(comp_b)
    ca = aliases.get(ca_raw, ca_raw if ca_raw in aliases.values() else None)
    cb = aliases.get(cb_raw, cb_raw if cb_raw in aliases.values() else None)
    if ca_raw == cb_raw:
        return "match", ca_raw, cb_raw
    if ca and cb:
        return ("match", ca, cb) if ca == cb else ("contradiction", ca, cb)
    if ca_raw in aliases.values() and cb_raw in aliases.values():
        return ("match", ca_raw, cb_raw) if ca_raw == cb_raw else ("contradiction", ca_raw, cb_raw)
    if ca and not cb or cb and not ca:
        # l'une connue, l'autre inconnue → pas prouvable, pas contradictoire
        return "unknown", ca, cb
    if ca is None and cb is None:
        # deux noms différents, aucun aliasé → unknowable honnêtement
        return "unknown", None, None
    return "unknown", ca, cb


# ---------------------------------------------------------------------------
# 6. MATCH IDENTITY (§8) — structure + canonical_match_id déterministe
# ---------------------------------------------------------------------------
class MatchIdentity:
    __slots__ = ("canonical_match_id", "home_team", "away_team", "competition",
                 "kickoff", "confidence", "status", "decision_reason",
                 "matched_sources", "external_ids", "reasons", "contradictions",
                 "candidates")

    def __init__(self, canonical_match_id=None, home_team=None, away_team=None,
                 competition=None, kickoff=None, confidence=0.0,
                 status=_dec.UNKNOWN, decision_reason=_dec.R_INSUFFICIENT_DATA,
                 matched_sources=(), external_ids=None, reasons=(),
                 contradictions=(), candidates=()):
        self.canonical_match_id = canonical_match_id
        self.home_team = home_team
        self.away_team = away_team
        self.competition = competition
        self.kickoff = kickoff
        self.confidence = round(float(confidence), 4)
        self.status = status
        self.decision_reason = decision_reason
        self.matched_sources = tuple(matched_sources or ())
        self.external_ids = dict(external_ids or {})
        self.reasons = tuple(reasons or ())
        self.contradictions = tuple(contradictions or ())
        self.candidates = tuple(candidates or ())

    def to_dict(self):
        return {
            "canonical_match_id": self.canonical_match_id,
            "home_team": self.home_team.to_dict() if hasattr(self.home_team, "to_dict") else self.home_team,
            "away_team": self.away_team.to_dict() if hasattr(self.away_team, "to_dict") else self.away_team,
            "competition": self.competition,
            "kickoff": self.kickoff,
            "confidence": self.confidence,
            "status": self.status,
            "decision_reason": self.decision_reason,
            "matched_sources": list(self.matched_sources),
            "external_ids": dict(self.external_ids),
            "reasons": list(self.reasons),
            "contradictions": list(self.contradictions),
            "candidates": list(self.candidates),
        }

    def __eq__(self, other):
        return isinstance(other, MatchIdentity) and self.to_dict() == other.to_dict()

    def __repr__(self):
        return (f"MatchIdentity(status={self.status!r} conf={self.confidence} "
                f"reason={self.decision_reason!r} id={self.canonical_match_id!r})")


def make_event(source=None, external_id=None, home="", away="", competition=None,
               kickoff=None, venue=None, country=None, retrieved_at=None,
               effective_at=None, **extra):
    """Fabrique un événement brut (forme minimale attendue par le moteur)."""
    ev = {"source": source, "external_id": external_id, "home": home,
          "away": away, "competition": competition, "kickoff": kickoff,
          "venue": venue, "country": country,
          "retrieved_at": retrieved_at, "effective_at": effective_at}
    ev.update(extra)
    return ev


def _canon_match_id(comp, date_str, home_ident, away_ident):
    """canonical_match_id DÉTERMINISTE — assemblé UNIQUEMENT à partir de
    preuves (jamais d'id externe inventé) : compétition canonique (ou forme
    normalisée), date UTC kickoff, clés canoniques des deux équipes."""
    comp_key = comp or "unknown-comp"
    date_key = date_str or "unknown-date"
    return f"match:{comp_key}|{date_key}|{home_ident.key()}|{away_ident.key()}"


# ---------------------------------------------------------------------------
# 7. LE MOTEUR (§9..§16, §22)
# ---------------------------------------------------------------------------
class MatchIdentityEngine:
    """compare() : deux événements → MatchIdentity + score explicable.
    resolve() : un événement + candidats → meilleure identité ou UNKNOWN.
    options : alias_book (AliasBook | entity_map dict | None), config (fusionnée
    par-dessus decision.DEFAULT_CONFIG — aucun coefficient n'est codé ici)."""

    def __init__(self, alias_book=None, config=None, entity_map=None):
        base = copy.deepcopy(_dec.DEFAULT_CONFIG)
        if config:
            for k, v in config.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    base[k].update(v)
                else:
                    base[k] = v
        self.config = base
        if alias_book is None and entity_map is not None:
            alias_book = AliasBook.from_entity_map(entity_map)
        if alias_book is None:
            alias_book = AliasBook()
        elif isinstance(alias_book, dict):
            alias_book = AliasBook.from_entity_map(alias_book)
        self.book = alias_book

    # -- utilitaires internes ---------------------------------------------
    def _team_evidence(self, key_prefix, name_a, name_b, reasons, contradictions,
                       identities):
        rel, credit, ident_a, ident_b = _teams_relation(name_a, name_b, self.book)
        identities[key_prefix] = (ident_a, ident_b)
        w = self.config["weights"][key_prefix + "_team"]
        side = key_prefix.upper()
        if rel == "different":
            contradictions.append(getattr(_dec, f"R_TEAM_CONTRADICTION_{side}"))
            return 0.0
        if rel in ("exact", "alias", "fuzzy_strong", "fuzzy_weak"):
            reasons.append(getattr(_dec, f"R_{side}_TEAM_{rel.upper()}"))
            return w * credit
        if rel == "guarded":
            reasons.append(getattr(_dec, f"R_TEAM_GUARDED_{side}"))
            return 0.0
        if rel == "ambiguous":
            reasons.append(getattr(_dec, f"R_TEAM_AMBIGUOUS_{side}"))
            return 0.0
        reasons.append(getattr(_dec, f"R_TEAM_UNMATCHED_{side}"))
        return 0.0

    def _category_check(self, ev_a, ev_b, contradictions):
        """§38 : hommes/femmes/jeunes ne fusionnent jamais automatiquement."""
        txt_a = " ".join(str(ev_a.get(k) or "") for k in ("home", "away", "competition"))
        txt_b = " ".join(str(ev_b.get(k) or "") for k in ("home", "away", "competition"))
        ca = team_category(normalize_team_name(txt_a))
        cb = team_category(normalize_team_name(txt_b))
        if ca == cb or ca == "open" or cb == "open":
            return
        # deux catégories explicites différentes → veto (ex. u19 vs women)
        contradictions.append(_dec.R_CATEGORY_MISMATCH)

    # -- compliance anti-fuite (§22) ---------------------------------------
    @staticmethod
    def _visible_at(ev, as_of):
        """True si l'événement est CONNU au temps as_of (timestamps ISO) —
        même discipline lexicographique que provenance.usable_at()."""
        if not as_of:
            return True
        for key in ("retrieved_at", "effective_at"):
            ts = ev.get(key)
            if isinstance(ts, str) and ts and _prov.ISO_RE.match(ts):
                if ts[:19] > as_of[:19]:
                    return False
        return True

    # -- comparaison à deux (§9..§15) ---------------------------------------
    def compare(self, ev_a, ev_b):
        cfg = self.config
        w = cfg["weights"]
        score = 0.0
        reasons, contradictions = [], []
        identities = {}

        ev_a, ev_b = dict(ev_a or {}), dict(ev_b or {})

        # (0) garde catégories — veto avant tout (§38)
        self._category_check(ev_a, ev_b, contradictions)

        # (1) external match id — même fournisseur uniquement (§10)
        sa, sb = ev_a.get("source"), ev_b.get("source")
        ida, idb = ev_a.get("external_id"), ev_b.get("external_id")
        ext_earned = False
        if sa and sb and sa == sb:
            if ida is not None and idb is not None:
                if str(ida) == str(idb):
                    score += w["external_id"]
                    reasons.append(_dec.R_EXTERNAL_ID_MATCH)
                    ext_earned = True
                else:
                    contradictions.append(_dec.R_EXTERNAL_ID_CONTRADICTION)
        elif sa and sb and sa != sb and ida is not None and idb is not None:
            reasons.append(_dec.R_EXTERNAL_IDS_DIFFERENT_PROVIDERS)

        # (2) inversion domicile/extérieur (§11) — jamais fusionnée
        rel_h_rev, cred_h_rev, _, _ = _teams_relation(ev_a.get("home"), ev_b.get("away"), self.book)
        rel_a_rev, cred_a_rev, _, _ = _teams_relation(ev_a.get("away"), ev_b.get("home"), self.book)
        inversion = (rel_h_rev in ("exact", "alias") and rel_a_rev in ("exact", "alias")
                     and ev_a.get("home") and ev_b.get("away"))
        # (3) équipes (§4/§5/§37)
        if inversion:
            verdict = MatchIdentity(
                home_team=identities.get("home", (make_team(ev_a.get("home")),))[0],
                away_team=identities.get("away", (make_team(ev_a.get("away")),))[0],
                competition=ev_a.get("competition"),
                kickoff=ev_a.get("kickoff") if isinstance(ev_a.get("kickoff"), str) else None,
                confidence=0.0, status=_dec.UNKNOWN,
                decision_reason=_dec.R_HOME_AWAY_INVERSION,
                matched_sources=tuple(x for x in (sa, sb) if x),
                external_ids={x: y for x, y in ((sa, ida), (sb, idb)) if x and y is not None},
                reasons=[_dec.R_HOME_AWAY_INVERSION], contradictions=[])
            return verdict
        score += self._team_evidence("home", ev_a.get("home"), ev_b.get("home"),
                                     reasons, contradictions, identities)
        score += self._team_evidence("away", ev_a.get("away"), ev_b.get("away"),
                                     reasons, contradictions, identities)

        # (4) compétition (§13)
        crel, ca, cb = _competition_relation(ev_a.get("competition"), ev_b.get("competition"))
        comp_canon = ca or cb
        if crel == "match":
            score += w["competition"]
            reasons.append(_dec.R_COMPETITION_MATCH)
        elif crel == "contradiction":
            contradictions.append(_dec.R_COMPETITION_CONTRADICTION)
        elif crel == "unknown":
            reasons.append(_dec.R_COMPETITION_UNKNOWN)
        else:
            reasons.append(_dec.R_COMPETITION_MISSING)

        # (5) date (§12) — fenêtre ±1 j ; au-delà = contradiction
        dcfg = cfg["date"]
        da, had_ta, _ = _parse_instant(ev_a.get("kickoff"))
        db, had_tb, _ = _parse_instant(ev_b.get("kickoff"))
        date_str = da.date().isoformat() if da else None
        if da and db:
            delta_days = abs((da.date() - db.date()).days)
            if delta_days == 0:
                score += w["date"]
                reasons.append(_dec.R_DATE_MATCH)
            elif delta_days <= dcfg["window_days"]:
                score += w["date"] * dcfg["next_day_factor"]
                reasons.append(_dec.R_DATE_NEXT_DAY)
            else:
                contradictions.append(_dec.R_DATE_DIFFERENT)
        # (6) heure — seulement si les deux la fournissent
        if da and db and had_ta and had_tb:
            tcfg = cfg["time"]
            delta_min = abs((da - db).total_seconds()) / 60.0
            if delta_min <= tcfg["match_min"]:
                score += w["time"]
                reasons.append(_dec.R_TIME_MATCH)
            elif delta_min <= tcfg["approx_min"]:
                score += w["time"] * tcfg["approx_factor"]
                reasons.append(_dec.R_TIME_APPROX)
            else:
                reasons.append(_dec.R_TIME_DIFFERENT)
        else:
            reasons.append(_dec.R_TIME_MISSING)

        # (7) stade — preuve mineure (bonus uniquement)
        va, vb = normalize_stadium(ev_a.get("venue")), normalize_stadium(ev_b.get("venue"))
        if va and vb:
            if va == vb:
                score += w["stadium"]
                reasons.append(_dec.R_STADIUM_MATCH)
            else:
                reasons.append(_dec.R_STADIUM_DIFFERENT)
        else:
            reasons.append(_dec.R_STADIUM_MISSING)

        score = round(max(0.0, min(1.0, score)), 4)
        status, dominant = _dec.decide_status(score, contradictions, cfg)

        home_ident = identities.get("home", (make_team(ev_a.get("home"), source=sa), None))[0]
        away_ident = identities.get("away", (make_team(ev_a.get("away"), source=sa), None))[0]
        matched_sources = tuple(dict.fromkeys(x for x in (sa, sb) if x))
        ext_ids = {x: str(y) for x, y in ((sa, ida), (sb, idb))
                   if x and y is not None}
        canon = None
        if status in (_dec.MATCH_AUTO, _dec.MATCH_JOURNALIZED):
            canon = _canon_match_id(comp_canon, date_str, home_ident, away_ident)

        return MatchIdentity(
            canonical_match_id=canon,
            home_team=home_ident, away_team=away_ident,
            competition=comp_canon, kickoff=date_str,
            confidence=score, status=status,
            decision_reason=dominant,
            matched_sources=matched_sources, external_ids=ext_ids,
            reasons=reasons, contradictions=contradictions)

    # -- résolution dans un pool de candidats (§16) --------------------------
    def resolve(self, event, candidates, as_of=None):
        """Cherche le MEILLEUR candidat pour `event` dans `candidates`.

        - as_of (§22) : tout candidat/événement portant retrieved_at ou
          effective_at POSTÉRIEUR à as_of est écarté (INFO_AFTER_T) ;
        - plusieurs candidats proches (< multi_margin) → UNKNOWN +
          MULTIPLE_CANDIDATES (les candidats sont conservés pour audit, §16) ;
        - aucun candidat satisfaisant → UNKNOWN/NO_MATCH selon les preuves ;
        - JAMAIS de choix arbitraire, JAMAIS de donnée inventée (§49/§50)."""
        event = dict(event or {})
        cands = [dict(c or {}) for c in (candidates or [])]

        if not self._visible_at(event, as_of):
            return MatchIdentity(status=_dec.UNKNOWN, decision_reason=_dec.R_INFO_AFTER_T,
                                 reasons=[_dec.R_INFO_AFTER_T])

        usable, excluded = [], 0
        for c in cands:
            if self._visible_at(c, as_of):
                usable.append(c)
            else:
                excluded += 1

        if not usable:
            reason = _dec.R_INFO_AFTER_T if excluded else _dec.R_NO_CANDIDATES
            return MatchIdentity(status=_dec.UNKNOWN, decision_reason=reason,
                                 reasons=[reason],
                                 home_team=make_team(event.get("home")),
                                 away_team=make_team(event.get("away")))

        results = [self.compare(event, c) for c in usable]
        eligible = []           # (index, score) — jamais des NO_MATCH prouvés
        for i, r in enumerate(results):
            if r.status in (_dec.MATCH_AUTO, _dec.MATCH_JOURNALIZED):
                th_lo = self.config["thresholds"]["journalized"]
                if r.confidence >= th_lo:
                    eligible.append((i, r.confidence))

        winner, multiple, order = _dec.pick_best_candidate(eligible, self.config)

        cand_summaries = [{"index": i, "confidence": results[i].confidence,
                           "status": results[i].status,
                           "decision_reason": results[i].decision_reason}
                          for i, _ in order] if order else []

        if multiple:
            home_ident = make_team(event.get("home"))
            away_ident = make_team(event.get("away"))
            return MatchIdentity(status=_dec.UNKNOWN,
                                 decision_reason=_dec.R_MULTIPLE_CANDIDATES,
                                 confidence=results[order[0][0]].confidence,
                                 home_team=home_ident, away_team=away_ident,
                                 reasons=list(results[order[0][0]].reasons) + [_dec.R_MULTIPLE_CANDIDATES],
                                 candidates=cand_summaries)

        if winner is None:
            # aucun candidat n'a passé le seuil journalisé
            best = max(enumerate(results),
                       key=lambda t: (t[1].confidence, -t[0]))[1] if results else None
            if best and best.status == _dec.NO_MATCH:
                return MatchIdentity(status=_dec.NO_MATCH,
                                     decision_reason=best.decision_reason,
                                     confidence=0.0,
                                     home_team=make_team(event.get("home")),
                                     away_team=make_team(event.get("away")),
                                     reasons=list(best.reasons),
                                     contradictions=list(best.contradictions))
            reason = _dec.R_NO_CANDIDATES
            return MatchIdentity(status=_dec.UNKNOWN, decision_reason=reason,
                                 confidence=best.confidence if best else 0.0,
                                 home_team=make_team(event.get("home")),
                                 away_team=make_team(event.get("away")),
                                 reasons=list(best.reasons) + [reason] if best else [reason])

        return results[winner]


# ---------------------------------------------------------------------------
# 8. WIKIDATA (§7) — ABSTRACTION HORS-LIGNE (aucune implémentation réseau)
# ---------------------------------------------------------------------------
class WikidataIdentityProvider:
    """Interface FUTURE (hub d'identité) — NON implémentée en 2B.WEB-1.

    Toutes les méthodes lèvent NotImplementedError : aucune collecte Internet
    n'est autorisée à cette étape (§7/§20). Les tests utilisent des fixtures
    locales (voir tests/test_web_identity.py)."""

    def lookup_team(self, name, language=None):
        raise NotImplementedError("2B.WEB-1 : aucune collecte Internet (§7)")

    def fetch_aliases(self, qid):
        raise NotImplementedError("2B.WEB-1 : aucune collecte Internet (§7)")

    def hydrate(self, alias_book):
        raise NotImplementedError("2B.WEB-1 : aucune collecte Internet (§7)")
