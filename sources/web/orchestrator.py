# -*- coding: utf-8 -*-
"""
RESEARCH ORCHESTRATOR (2B.WEB-3 §3/§12/§16/§23/§24/§31/§32)
============================================================
Pipeline complet PAR MATCH :

    MATCH → IDENTITY (WEB-1) → DISCOVERY (registry) → COMPLIANCE (WEB-2)
    → SAFE HTTP (WEB-2) → ADAPTERS/EXTRACTORS → NORMALIZED → PROVENANCE
    → VALIDATION → CONFLICTS → FRESHNESS → POINT-IN-TIME STORE
    → SNAPSHOT BUILDER 2A (séparé : snapshots.py)

Règles dures :
- budget requêtes : objectif ≤ 8 / match / cycle, HARD CAP 20 (§31) ;
- concurrence ≤ 3 (déléguée à SafeHttpClient.fetch_many) ;
- JAMAIS de blocage du LIVE : ce pipeline n'est pas appelé par le live
  existant (additif) ; en cas de dépassement de délai → retourne ce qui
  est déjà disponible ;
- FALLBACK CONTRÔLÉ (§23) : chaîne registry, essai suivant UNIQUEMENT après
  échec du précédent, événement FALLBACK_USED + provenance = source réelle ;
- RÉSILIENCE (§32) : une source en panne → UNKNOWN pour ses données, le
  reste continue ; aucune exception ne sort de research_match ;
- AUCUN appel réseau hors SafeHttpClient.
"""
from concurrent.futures import ThreadPoolExecutor

from sources import registry as _regmod
from .normalized import UNKNOWN, unknown_point, now_iso
from .extractors.base import FetchContext
from .pit_store import PointInTimeStore
from .research_journal import ResearchJournal
from .quality_metrics import QualityMetrics
from . import conflicts as _conflicts
from .adapters import (EspnAdapter, FootballDataAdapter, OpenMeteoAdapter,
                       OpenLigaDBAdapter, StatsBombAdapter, WikidataAdapter)

DEFAULT_BUDGET = 8
HARD_CAP = 20

#: priorités des adapters (§31) — P0 vital … P3 enrichissement
PRIORITIES = {"espn": {"scoreboard": 0, "summary": 1, "standings": 1},
              "football_data_co_uk": {"season_csv": 1},
              "open_meteo": {"weather": 2},
              "openligadb": {"matches": 1},
              "statsbomb_open": {"competitions": 3, "matches": 3, "events": 3},
              "wikidata": {"search": 3, "entity": 3}}


class ResearchReport:
    def __init__(self, match_id, as_of):
        self.match_id = match_id
        self.as_of = as_of
        self.points = []                        # DataPoint (valides + UNKNOWN)
        self.requests_used = 0
        self.fallbacks = []                     # [{data_type, from, to, why}]
        self.errors = []                        # [{source, kind, code}]
        self.conflicts = []                     # rapports conflicts.py
        self.identity = None                    # décision identity engine
        self.skipped = []                       # specs non exécutées (budget)

    def to_dict(self):
        return {
            "match_id": self.match_id, "as_of": self.as_of,
            "datapoints": [p.to_dict(self.as_of) for p in self.points],
            "requests_used": self.requests_used, "fallbacks": self.fallbacks,
            "errors": self.errors,
            "conflicts": self.conflicts, "identity": self.identity,
            "skipped": self.skipped,
            "unknown_count": sum(1 for p in self.points
                                 if p.value is UNKNOWN),
            "valid_count": sum(1 for p in self.points if p.valid),
        }


class ResearchOrchestrator:
    """Prépare les adapters et exécute le pipeline (aucune I/O hors client)."""

    def __init__(self, client, registry=None, store=None, journal=None,
                 metrics=None, budget=DEFAULT_BUDGET, hard_cap=HARD_CAP):
        self.client = client
        self.reg = registry or getattr(client, "reg", None) or _regmod.load()
        self.store = store or PointInTimeStore()
        self.journal = journal or ResearchJournal()
        self.metrics = metrics or QualityMetrics()
        self.budget = int(budget)
        self.hard_cap = min(int(hard_cap), HARD_CAP)
        self.adapters = {
            "espn": EspnAdapter(client, self.reg, self.journal, self.metrics),
            "football_data_co_uk": FootballDataAdapter(client, self.reg,
                                                       self.journal,
                                                       self.metrics),
            "open_meteo": OpenMeteoAdapter(client, self.reg, self.journal,
                                           self.metrics),
            "openligadb": OpenLigaDBAdapter(client, self.reg, self.journal,
                                            self.metrics),
            "statsbomb_open": StatsBombAdapter(client, self.reg, self.journal,
                                               self.metrics),
            "wikidata": WikidataAdapter(client, self.reg, self.journal,
                                        self.metrics)}

    # ------------------------------------------------------------------ pipeline
    def research_match(self, match_ctx, as_of=None,
                       sources=None, conflict_keys=("score_home", "score_away")):
        """match_ctx : dict(match_id, home, away, competition|league_name,
        kickoff, league_code, event_id?, fd_league/fd_season?, lat/lon?,
        ol_slug/ol_season?, sb_*?, wikidata_query?) extras → FetchContext.
        Retourne TOUJOURS un ResearchReport (jamais d'exception — §32)."""
        as_of = as_of or match_ctx.get("as_of") or now_iso()
        mid = match_ctx.get("match_id")
        report = ResearchReport(mid, as_of)
        extras = {k: v for k, v in dict(match_ctx).items()
                  if k not in ("match_id", "retrieved_at", "source_url",
                               "team_id", "player_id", "competition_id",
                               "confidence", "as_of")}
        ctx = FetchContext(match_id=mid, retrieved_at=now_iso(),
                           source_url=None, **extras)
        wanted = sources or list(self.adapters)
        # 1) construire toutes les specs, triées par priorité
        specs = []
        for sid in wanted:
            ad = self.adapters.get(sid)
            if ad is None:
                continue
            try:
                for spec in ad.build_requests(ctx):
                    prio = PRIORITIES.get(sid, {}).get(spec["kind"], 3)
                    specs.append((prio, sid, spec))
            except Exception as e:               # adapter résilient (§32)
                report.errors.append({"source": sid, "kind": "build_requests",
                                      "code": type(e).__name__})
        specs.sort(key=lambda t: (t[0], t[1]))
        # 2) budget dur
        allowed, skipped = specs[:self.hard_cap], specs[self.hard_cap:]
        report.skipped = [{"source": s, "kind": sp["kind"]}
                          for _, s, sp in skipped]
        # 3) exécution — par vague de priorité, fetch_many ≤3 DANS la vague
        by_prio = {}
        for prio, sid, spec in allowed:
            if len(by_prio) >= self.budget and prio not in by_prio and \
                    sum(len(v) for v in by_prio.values()) >= self.budget:
                report.skipped.append({"source": sid, "kind": spec["kind"]})
                continue
            by_prio.setdefault(prio, []).append((sid, spec))
        for prio in sorted(by_prio):
            wave = by_prio[prio]
            for sid, spec in wave:
                if report.requests_used >= self.hard_cap:
                    report.skipped.append({"source": sid,
                                           "kind": spec["kind"]})
                    continue
                res = self.adapters[sid].fetch_and_parse(ctx, spec)
                report.requests_used += (0 if res.get("cache_hit") else 1)
                if not res["ok"]:
                    report.errors.append({"source": sid, "kind": spec["kind"],
                                          "code": res["error"]})
                    fb = self._try_fallback(ctx, report, sid, spec)
                    if fb is not None:
                        report.points += fb
                    continue
                report.points += res["points"]
                if sid == "espn" and spec["kind"] == "scoreboard":
                    self._capture_identity(res, report)
        # 4) résolution des conflits (valeurs CONSERVÉES, pas de moyenne)
        for dtype in conflict_keys:
            pts = [p for p in report.points if p.data_type == dtype]
            if len({p.source for p in pts}) > 1:
                chosen, crep = _conflicts.resolve_conflict(
                    pts, dtype, reg=self.reg, journal=self.journal,
                    metrics=self.metrics, match_id=mid)
                report.conflicts.append(crep)
        # 5) magasin point-in-time (anti-leakage à la lecture)
        self.store.add_many(report.points)
        self.journal.record(event="RESEARCH_CYCLE_DONE", match_id=mid,
                            status="DONE",
                            records_found=len(report.points),
                            decision={"requests_used": report.requests_used})
        return report

    # ------------------------------------------------------------------ helpers
    def _try_fallback(self, ctx, report, sid, spec):
        """§23 — fallback EXPLICITE : source suivante de la chaîne registry,
        provenance = la source qui a réellement répondu, journal visible."""
        dtype = spec.get("data_type", "score_live")
        try:
            chain = _regmod.fallback_chain(self.reg, dtype)
        except Exception:
            chain = []
        ids = [s["source_id"] if isinstance(s, dict) else s for s in chain]
        if sid not in ids:
            return None
        nxt = None
        for cand in ids[ids.index(sid) + 1:]:
            if cand in self.adapters and cand != sid:
                nxt = cand
                break
        if nxt is None:
            if report is not None:
                self.journal.record(event="FALLBACK_NONE",
                                    match_id=ctx.match_id, source=sid,
                                    data_type=dtype, status="UNKNOWN",
                                    decision="NO_SOURCE_LEFT")
            return None
        ad = self.adapters[nxt]
        specs = ad.build_requests(ctx)
        if not specs:
            self.journal.record(event="FALLBACK_NONE",
                                match_id=ctx.match_id, source=sid,
                                data_type=dtype, status="UNKNOWN",
                                decision=f"{nxt}_NO_REQUESTS")
            return None
        res = ad.fetch_and_parse(ctx, specs[0])
        report.fallbacks.append({"data_type": dtype, "from": sid, "to": nxt,
                                 "why": f"{sid} indisponible"})
        self.metrics.record_fallback()
        self.journal.record(
            event="FALLBACK_USED", match_id=ctx.match_id, source=nxt,
            data_type=dtype, status="FALLBACK_USED",
            decision={"from": sid, "to": nxt, "provenance": nxt})
        return res["points"]

    def _capture_identity(self, res, report):
        for dp in res["points"]:
            if dp.data_type == "identity_decision" and \
                    isinstance(dp.value, dict):
                report.identity = dp.value
                self.metrics.record_identity(dp.value.get("status"))
                return
