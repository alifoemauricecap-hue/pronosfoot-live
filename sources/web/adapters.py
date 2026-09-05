# -*- coding: utf-8 -*-
"""
SOURCE ADAPTERS (2B.WEB-3 §4)
==============================
Interface commune source :

    SOURCE → (SafeHttpClient.fetch) → RAW RESPONSE → EXTRACTOR (pur)
    → NORMALIZED DATA → PROVENANCE (DataPoint) → VALIDATION → STORE

RÈGLE D'INJECTION : l'adapter reçoit un SafeHttpClient (WEB-2). Il n'a
JAMAIS le droit d'appeler urllib/requests/httpx — le client est le SEUL
chemin réseau (§27). Les parseurs sont injectables pour des tests 100 %
hors ligne.
"""
from sources import registry as _regmod
from .normalized import UNKNOWN, unknown_point
from .validation import apply_validation
from .response import SafeHttpError


class SourceAdapter:
    """Base — une sous-classe par famille de source."""

    source_id = None
    #: data_type registry utilisé pour les requêtes de l'adapter
    request_data_type = "score_live"

    def __init__(self, client, registry=None, journal=None, metrics=None):
        self.client = client                  # SafeHttpClient OBLIGATOIRE
        self.reg = registry or getattr(client, "reg", None) or _regmod.load()
        self.journal = journal
        self.metrics = metrics

    # -- capacités réelles (registry) -----------------------------------------
    def capabilities(self):
        src = _regmod.get_source(self.reg, self.source_id)
        return dict((src or {}).get("capabilities") or {})

    # -- requêtes à fabriquer pour un contexte donné ---------------------------
    def build_requests(self, ctx):
        """→ liste de specs {kind, url, data_type} (à définir par sous-classe)."""
        raise NotImplementedError

    # -- extraction (pure) ------------------------------------------------------
    def parse_body(self, kind, body, ctx):
        raise NotImplementedError

    # -- cycle complet fetch+parse (résilient, journalisé) ----------------------
    def fetch_and_parse(self, ctx, spec):
        """Une requête → DataPoints. Toute SafeHttpError devient résultat
        d'erreur explicite (jamais d'exception propagée au pipeline — §32)."""
        kind = spec["kind"]
        try:
            resp = self.client.fetch(self.source_id, spec["url"],
                                     spec.get("data_type",
                                              self.request_data_type),
                                     allow_cache=True)
        except SafeHttpError as e:
            if self.metrics:
                self.metrics.record_fetch(ctx.match_id, ok=False)
            if self.journal:
                self.journal.record(
                    event="RESEARCH_ERROR", match_id=ctx.match_id,
                    source=self.source_id, data_type=kind,
                    host=_host(spec["url"]), status="ERROR", error=e.code)
            return {"kind": kind, "ok": False, "error": e.code, "points": []}
        if self.metrics:
            self.metrics.record_fetch(ctx.match_id, ok=True,
                                      latency_ms=resp.latency_ms,
                                      cache_hit=resp.cache_hit)
        try:
            points = self.parse_body(kind, resp.body, ctx)
        except Exception as pe:                        # parse jamais fatal
            if self.journal:
                self.journal.record(
                    event="PARSE_ERROR", match_id=ctx.match_id,
                    source=self.source_id, data_type=kind,
                    host=_host(spec["url"]), status="PARSE_ERROR",
                    error=type(pe).__name__)
            return {"kind": kind, "ok": False, "error": "PARSE_ERROR",
                    "points": []}
        for dp in points:
            apply_validation(dp)
            if self.metrics:
                self.metrics.record_datapoint(
                    unknown=(dp.value is UNKNOWN), valid=dp.valid)
        if self.journal:
            self.journal.record(
                event="RESEARCH_OK", match_id=ctx.match_id,
                source=self.source_id, data_type=kind,
                host=_host(spec["url"]), status="OK",
                latency_ms=resp.latency_ms,
                bytes=resp.content_length, cache_hit=resp.cache_hit,
                records_found=len(points), confidence=ctx.confidence)
        return {"kind": kind, "ok": True, "error": None, "points": points,
                "cache_hit": resp.cache_hit}

    def unknown_coverage(self, ctx, kinds, reason):
        """UNKNOWN explicite pour les types demandés mais indisponibles (§R1)."""
        return [unknown_point(k, self.source_id, ctx.retrieved_at,
                              match_id=ctx.match_id,
                              issues=[reason]) for k in kinds]


def _host(url):
    try:
        from urllib.parse import urlsplit
        return urlsplit(url).hostname
    except Exception:
        return None


# ===========================================================================
# ADAPTERS CONCRETS — une classe par famille de source autorisée (§2)
# ===========================================================================
from .extractors import espn as _espn_x
from .extractors import football_data as _fd_x
from .extractors import open_meteo as _om_x
from .extractors import openligadb as _ol_x
from .extractors import statsbomb as _sb_x
from .extractors import wikidata as _wd_x
from .extractors.base import ensure_obj, FetchContext


class EspnAdapter(SourceAdapter):
    source_id = "espn"
    request_data_type = "score_live"

    def __init__(self, client, registry=None, journal=None, metrics=None,
                 identity_engine=None):
        super().__init__(client, registry, journal, metrics)
        self.engine = identity_engine

    def build_requests(self, ctx):
        base = _regmod.get_source(self.reg, self.source_id)["base_url"] \
            .rstrip("/")
        league = ctx.extras.get("league_code")
        reqs = []
        if league:
            reqs.append({"kind": "scoreboard",
                         "url": f"{base}/{league}/scoreboard",
                         "data_type": "score_live"})
            reqs.append({"kind": "standings",
                         "url": f"{base}/{league}/standings",
                         "data_type": "standings"})
        event_id = ctx.extras.get("event_id")
        if league and event_id:
            reqs.append({"kind": "summary",
                         "url": f"{base}/{league}/summary?event={event_id}",
                         "data_type": "match_stats"})
        return reqs

    def pick_event(self, obj, ctx):
        """§12 — MATCH IDENTITY : sélectionne L'ÉVÉNÉMENT du match cible.
        Retourne (event|None, identity_status, confidence). Aucune fusion
        arbitraire : NO_MATCH/UNKNOWN → None + statut explicite."""
        from . import identity as _id
        from . import decision as _dec
        events = (obj or {}).get("events") or []
        if not events or not (ctx.extras.get("home") and
                              ctx.extras.get("away")):
            return None, _dec.NO_MATCH if not events else _dec.UNKNOWN, 0.0
        engine = self.engine or _id.MatchIdentityEngine()
        comp_probe = ctx.extras.get("league_name") or \
            ctx.extras.get("league_code")
        probe = _id.make_event(
            source="probe", external_id=None,
            home=ctx.extras.get("home"), away=ctx.extras.get("away"),
            competition=comp_probe,
            kickoff=ctx.extras.get("kickoff"))
        scored = []
        for i, ev in enumerate(events):
            home, away = _espn_x._home_away(ev)
            cand = _id.make_event(
                source="espn", external_id=ev.get("id"),
                home=(home or {}).get("team", {}).get("displayName", ""),
                away=(away or {}).get("team", {}).get("displayName", ""),
                competition=comp_probe,     # même forme que le probe (équité)
                kickoff=ev.get("date"),
                venue=((ev.get("competitions") or [{}])[0]
                       .get("venue") or {}).get("fullName"))
            ident = engine.compare(probe, cand)
            if ident.status != _dec.NO_MATCH:
                scored.append((i, ident.confidence))
        winner, multiple, _order = _dec.pick_best_candidate(scored)
        if winner is None:
            return None, (_dec.UNKNOWN if scored else _dec.NO_MATCH), 0.0
        best = next((s for s in scored if s[0] == winner), (None, 0.0))
        status, _reason = _dec.decide_status(best[1], ())   # seuils WEB-1
        if status in (_dec.NO_MATCH, _dec.UNKNOWN):
            return None, status, best[1]           # pas de fusion douteuse
        return events[winner], status, best[1]

    def parse_body(self, kind, body, ctx):
        obj = ensure_obj(body)
        if kind == "scoreboard":
            ev, status, conf = self.pick_event(obj, ctx)
            from .extractors.base import point as bpoint
            from .normalized import SOURCE_NATIVE
            out = [bpoint({"status": status, "confidence": round(conf, 4)},
                          "identity_decision", self.source_id, ctx,
                          level=SOURCE_NATIVE, confidence="high")]
            if ev is None:
                return out
            out += _espn_x.parse_event_identity(ev, ctx)
            out += _espn_x.parse_event_live(ev, ctx)
            return out
        if kind == "summary":
            return (_espn_x.parse_summary_stats(obj, ctx)
                    + _espn_x.parse_summary_lineups(obj, ctx)
                    + _espn_x.parse_summary_injuries(obj, ctx))
        if kind == "standings":
            return _espn_x.parse_standings(obj, ctx)
        raise ValueError(f"kind inconnu : {kind}")


class FootballDataAdapter(SourceAdapter):
    source_id = "football_data_co_uk"
    request_data_type = "historical_results"

    def build_requests(self, ctx):
        league = ctx.extras.get("fd_league")           # ex. "E0"
        season = ctx.extras.get("fd_season")           # ex. "2526"
        if not (league and season):
            return []
        base = _regmod.get_source(self.reg, self.source_id)["base_url"] \
            .rstrip("/")
        return [{"kind": "season_csv",
                 "url": f"{base}/{season}/{league}.csv",
                 "data_type": "historical_results"}]

    @staticmethod
    def _resolve_csv_team(csv_names, target):
        """§12 — rattache le nom demandé (ESPN : « Newcastle United ») au nom
        CSV (fd.co.uk : « Newcastle ») via l'Identity Engine WEB-1 :
        1) égalité normalisée, 2) préfixe UNIVOQUE, sinon None (ambiguïté
        ⇒ UNKNOWN, jamais de fusion arbitraire)."""
        from . import identity as _id
        if not target:
            return None
        t_norm = _id.normalize_team_name(target)
        exact = [n for n in csv_names
                 if _id.normalize_team_name(n) == t_norm]
        if len(exact) == 1:
            return exact[0]
        pref = [n for n in csv_names
                if _id.normalize_team_name(n).startswith(t_norm)
                or t_norm.startswith(_id.normalize_team_name(n))]
        if len(pref) == 1:
            return pref[0]
        return None                                   # 0 ou ambigu → UNKNOWN

    def parse_body(self, kind, body, ctx):
        if kind != "season_csv":
            raise ValueError(f"kind inconnu : {kind}")
        rows, _dps = _fd_x.parse_csv(body, ctx)
        from .aggregation import form_datapoints
        out = []
        home_t, away_t = ctx.extras.get("home"), ctx.extras.get("away")
        if rows:
            from .extractors.base import point as bpoint
            out.append(bpoint(len(rows), "historical_matches_rows",
                              self.source_id, ctx, level="SOURCE_NATIVE"))
        csv_names = {r.get("home") for r in rows} | \
                    {r.get("away") for r in rows}
        csv_names.discard(None)
        mapped = {t: self._resolve_csv_team(csv_names, t)
                  for t in (home_t, away_t) if t}
        for team in (home_t, away_t):
            if not team:
                continue
            csv_team = mapped.get(team)
            dp = form_datapoints(rows, csv_team or team, ctx,
                                 self.source_id, last_n=5)
            if csv_team is None:
                dp.issues.append("TEAM_NAME_NOT_MAPPED")
            out.append(dp)
            dp10 = form_datapoints(rows, csv_team or team, ctx,
                                   self.source_id, last_n=10)
            out.append(dp10)
        self.rows = rows                              # dispo pour le pipeline
        # résultat H2H réellement présent dans le CSV (noms résolus)
        h2h = self._h2h(rows, mapped.get(home_t) or home_t,
                        mapped.get(away_t) or away_t)
        if h2h:
            from .normalized import AGGREGATED, MODEL_VERSION
            from .extractors.base import point as bpoint
            out.append(bpoint(
                h2h["metrics"], "h2h", self.source_id, ctx,
                level=AGGREGATED, derivation_method="h2h_from_real_csv",
                model_version=MODEL_VERSION, inputs=h2h["inputs"]))
        return out

    @staticmethod
    def _h2h(rows, home, away, last_n=10):
        if not (home and away):
            return None
        games = []
        for r in sorted(rows, key=lambda x: x.get("date") or "",
                        reverse=True):
            if {r.get("home"), r.get("away")} == {home, away}:
                if r.get("fthg") is None:
                    continue
                games.append({"date": r.get("date"), "home": r["home"],
                              "away": r["away"], "score":
                              f"{r['fthg']}-{r['ftag']}"})
            if len(games) >= last_n:
                break
        if not games:
            return None
        hw = sum(1 for g in games if g["home"] == home
                 and int(g["score"].split("-")[0]) >
                 int(g["score"].split("-")[1]))
        aw = sum(1 for g in games if g["away"] == home
                 and int(g["score"].split("-")[1]) >
                 int(g["score"].split("-")[0]))
        return {"metrics": {"meetings": len(games),
                            "wins_for_first_team": hw + aw,
                            "games": games}, "inputs": games}


class OpenMeteoAdapter(SourceAdapter):
    source_id = "open_meteo"
    request_data_type = "weather"

    def build_requests(self, ctx):
        lat, lon = ctx.extras.get("lat"), ctx.extras.get("lon")
        if lat is None or lon is None:
            return []                                   # §7 : jamais de ville approximative
        base = _regmod.get_source(self.reg, self.source_id)["base_url"] \
            .rstrip("/")
        url = (f"{base}/forecast?latitude={lat}&longitude={lon}"
               f"&hourly=temperature_2m,apparent_temperature,"
               f"relative_humidity_2m,precipitation,"
               f"precipitation_probability,wind_speed_10m,weather_code")
        return [{"kind": "weather", "url": url, "data_type": "weather"}]

    def parse_body(self, kind, body, ctx):
        if kind != "weather":
            raise ValueError(f"kind inconnu : {kind}")
        return _om_x.parse_weather(body, ctx,
                                   target_time=ctx.extras.get("kickoff"))


class OpenLigaDBAdapter(SourceAdapter):
    source_id = "openligadb"
    request_data_type = "match_calendar"

    def build_requests(self, ctx):
        slug = ctx.extras.get("ol_slug")               # ex. "bl1"
        season = ctx.extras.get("ol_season")           # ex. "2025"
        if not (slug and season):
            return []
        base = _regmod.get_source(self.reg, self.source_id)["base_url"] \
            .rstrip("/")
        return [{"kind": "matches",
                 "url": f"{base}/getmatchdata/{slug}/{season}",
                 "data_type": "match_calendar"}]

    def parse_body(self, kind, body, ctx):
        if kind != "matches":
            raise ValueError(f"kind inconnu : {kind}")
        points, rows = _ol_x.parse_matches(body, ctx)
        self.rows = rows
        return points


class StatsBombAdapter(SourceAdapter):
    source_id = "statsbomb_open"
    request_data_type = "historical_results"

    def build_requests(self, ctx):
        base = _regmod.get_source(self.reg, self.source_id)["base_url"] \
            .rstrip("/")
        reqs = [{"kind": "competitions", "url": f"{base}/competitions.json",
                 "data_type": "historical_results"}]
        comp, season = ctx.extras.get("sb_competition_id"), \
            ctx.extras.get("sb_season_id")
        if comp and season:
            reqs.append({"kind": "matches",
                         "url": f"{base}/matches/{comp}/{season}.json",
                         "data_type": "historical_results"})
        if ctx.extras.get("sb_match_id"):
            reqs.append({"kind": "events",
                         "url": f"{base}/events/{ctx.extras['sb_match_id']}.json",
                         "data_type": "match_stats"})
        return reqs

    def parse_body(self, kind, body, ctx):
        obj = ensure_obj(body)
        if kind == "competitions":
            return _sb_x.parse_competitions(obj, ctx)
        if kind == "matches":
            points, rows = _sb_x.parse_matches(obj, ctx)
            self.rows = rows
            return points
        if kind == "events":
            return _sb_x.derive_match_stats(
                obj, ctx, match_date=ctx.extras.get("sb_match_date"))
        raise ValueError(f"kind inconnu : {kind}")


class WikidataAdapter(SourceAdapter):
    source_id = "wikidata"
    request_data_type = "entity_identity"

    def build_requests(self, ctx):
        q = ctx.extras.get("wikidata_query")            # nom d'équipe
        if not q:
            return []
        base = _regmod.get_source(self.reg, self.source_id)["base_url"] \
            .rstrip("/")
        from urllib.parse import quote
        return [{"kind": "search",
                 "url": (f"{base}/w/api.php?action=wbsearchentities"
                         f"&search={quote(q)}&language=fr&format=json"),
                 "data_type": "entity_identity"}]

    def parse_body(self, kind, body, ctx):
        obj = ensure_obj(body)
        if kind == "search":
            return _wd_x.parse_search(obj, ctx)
        if kind == "entity":
            return _wd_x.parse_entity(obj, ctx)
        raise ValueError(f"kind inconnu : {kind}")
