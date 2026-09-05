# -*- coding: utf-8 -*-
"""
INGESTION SCHEDULER (2B.WEB-4 §7/§8/§9/§10/§11/§23/§35/§37)
============================================================
Un SEUL scheduler interne (thread daemon), JAMAIS de service payant :

    tick (config) → cycles REGROUPÉS (IngestionCycle, §8) →
    ORCHESTRATOR WEB-3 (budget 8/match, hard cap 20 — §10) →
    PIT STORE persistant → CANDIDATS T-180/T-60/T-15 (bridge, kickoff
    protection) → métriques + alertes.

RESTART-SAFE (§23/§37 Render free) :
- l'état (derniers cycles) vit dans web_scheduler_state / web_ingestion_cycles ;
- au boot : recover_on_boot() relance un cycle RECOVERY si le dernier est
  trop vieux — AUCUNE donnée n'est perdue (cache/PIT/journal persistants).

RÈGLES :
- P0 identité/calendrier/score … P3 enrichissements (orchestrateur, §9) ;
- un échec P0 n'arrête PAS le cycle (§9) : le match reste honnêtement
  marqué par ses erreurs ; jamais de blocage du LIVE (thread à part, §25) ;
- LIVE : désactivé par défaut (config) — le live reste au chemin ESPN 2A ;
- MATCH TERMINÉ : rien (settlement 2A uniquement — §16).
"""
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import db as _db

from ..normalized import now_iso
from . import bridge as _bridge
from . import metrics as _metrics
from .config import WEB4_CONFIG, TIERS
from .entity_store import EntityStore, team_key
from .venues import VenueStore

UTC = timezone.utc

#: correspondances de compétitions (uniquement celles PROUVÉES par source —
#: football-data.co.uk : grands championnats européens ; OpenLigaDB :
#: compétitions ALLEMANDES uniquement — jamais global, règle WEB-3).
FD_LEAGUE = {
    "eng.1": "E0", "eng.2": "E1", "eng.3": "E2",
    "esp.1": "SP1", "esp.2": "SP2", "ita.1": "I1", "ita.2": "I2",
    "fra.1": "F1", "fra.2": "F2", "ger.1": "D1", "ger.2": "D2",
    "por.1": "P1", "ned.1": "N1", "bel.1": "B1", "sco.1": "SC0",
    "tur.1": "T1", "gre.1": "G1",
}
OL_SLUG = {"ger.1": "bl1", "ger.2": "bl2"}


def _parse_iso(s):
    return datetime.fromisoformat((s or "").replace("Z", "+00:00"))


def fd_season_code(d):
    """Code saison football-data (« 2627 » = 2026-2027). Saison commence
    en juillet : mois ≥ 7 ⇒ année courante+1, sinon année-1+année."""
    y = d.year
    if d.month >= 7:
        return f"{y % 100:02d}{(y + 1) % 100:02d}"
    return f"{(y - 1) % 100:02d}{y % 100:02d}"


def ol_season(d):
    """Année de saison OpenLigaDB (ex. 2026 pour 2026-2027)."""
    return str(d.year if d.month >= 7 else d.year - 1)


class IngestionCycleError(Exception):
    pass


class IngestionScheduler:
    def __init__(self, orchestrator_factory, store, journal, metrics=None,
                 config=None, db_module=None, entity_store=None,
                 venue_store=None, registry=None, clock=None):
        self.orchestrator_factory = orchestrator_factory
        self.store = store
        self.journal = journal
        self.db = db_module or _db
        self.metrics = metrics or _metrics.IngestionMetrics(db_module=self.db)
        self.config = config or WEB4_CONFIG
        self.entities = entity_store or EntityStore(db_module=self.db)
        self.venues = venue_store or VenueStore(db_module=self.db)
        self.registry = registry
        self.clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event()

    # ---------------------------------------------------------------- phases §11
    def phase_for(self, kickoff_iso, now=None):
        """Phase du match à `now` (§11 planification intelligente)."""
        now = now or self.clock()
        ko = _parse_iso(kickoff_iso)
        delta_min = (ko - now).total_seconds() / 60.0
        ph = self.config["phases"]
        if ko <= now:
            return "LIVE" if (now - ko) <= timedelta(minutes=150) else "POST"
        if delta_min <= ph["t15_min"]:
            return "T-15"
        if delta_min <= ph["t60_min"]:
            return "T-60"
        if delta_min <= ph["t180_min"]:
            return "T-180"
        return "HORIZON"

    # ---------------------------------------------------------------- sélection
    def select_upcoming(self, now=None, limit=None):
        now = now or self.clock()
        horizon = now + timedelta(hours=self.config["horizon_hours"])
        limit = int(limit or self.config["matches_per_cycle"])
        rows = self.db.rows_to_dicts(self.db.query(
            """SELECT * FROM matches
               WHERE status='UPCOMING' AND kickoff_time_utc >= %s
                 AND kickoff_time_utc <= %s
               ORDER BY kickoff_time_utc LIMIT %s""",
            (now.isoformat(), horizon.isoformat(), limit)))
        return rows

    # ---------------------------------------------------------------- état §37
    def _state_get(self, key):
        r = self.db.query("SELECT value_json FROM web_scheduler_state "
                          "WHERE key=%s", (key,), one=True)
        if not r or not r["value_json"]:
            return None
        try:
            return json.loads(r["value_json"])
        except Exception:
            return None

    def _state_set(self, key, value):
        self.db.execute(
            """INSERT INTO web_scheduler_state (key, value_json, updated_at)
               VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
               updated_at=excluded.updated_at""",
            (key, json.dumps(value, ensure_ascii=False, default=str),
             self.db.utcnow()))

    def due(self, tier, now=None):
        """Un cycle du tier est-il dû (fréquence ≥ config) ?"""
        now = now or self.clock()
        freq = self.config["frequencies_sec"].get(tier, 900)
        last = self._state_get(f"last_cycle:{tier}")
        if not last:
            return True
        try:
            return (now - _parse_iso(last)).total_seconds() >= freq
        except Exception:
            return True

    # ---------------------------------------------------------------- match_ctx
    def build_ctx(self, match_row, now=None):
        """Construit le match_ctx de l'orchestrateur depuis une ligne
        `matches` 2A + référentiels (venues → météo UNIQUEMENT si prouvée)."""
        now = now or self.clock()
        comp = match_row.get("competition") or ""
        ctx = {
            "match_id": match_row["id"],
            "home": match_row["home_team"],
            "away": match_row["away_team"],
            "kickoff": match_row["kickoff_time_utc"],
            "league_code": comp,
            "league_name": comp,
            "wikidata_query": match_row["home_team"],
        }
        if match_row.get("source") == "espn" and match_row.get("source_match_id"):
            ctx["event_id"] = match_row["source_match_id"]
        fd = FD_LEAGUE.get(comp)
        if fd:
            ctx["fd_league"] = fd
            ctx["fd_season"] = fd_season_code(now)
        ol = OL_SLUG.get(comp)
        if ol:
            ctx["ol_slug"] = ol
            ctx["ol_season"] = ol_season(now)
        # §19 — météo : UNIQUEMENT si le stade est prouvé (lat/lon sourceés)
        venue_name = match_row.get("venue")
        if venue_name:
            coords = self.venues.coords_for(venue_name)
            if coords:
                ctx["lat"], ctx["lon"] = coords
        return ctx

    # ---------------------------------------------------------------- cycles §8
    def run_cycle(self, trigger="SCHEDULED", tier=None, now=None,
                  matches=None, activate_candidates=True):
        """Exécute UN cycle d'ingestion regroupé. NE LÈVE JAMAIS (§22)."""
        now = now or self.clock()
        cycle_id = uuid.uuid4().hex[:16]
        started_at = now_iso()
        t0 = time.monotonic()
        summary = {"cycle_id": cycle_id, "trigger": trigger, "tier": tier,
                   "started_at": started_at, "matches": [], "candidates": [],
                   "requests": 0, "errors": [], "skipped": [],
                   "identity_ok": 0, "identity_unknown": 0}
        self._cycle_insert(cycle_id, trigger, tier, started_at)
        try:
            rows = matches if matches is not None else self._matches_for_tier(
                tier, now)
            orchestrator = self.orchestrator_factory()
            cap = int(self.config["requests_per_cycle_cap"])
            for match_row in rows:
                if summary["requests"] >= cap:
                    summary["skipped"].append(
                        {"match_id": match_row["id"], "reason": "CYCLE_CAP"})
                    continue
                self._research_one(orchestrator, match_row, now, summary,
                                   activate_candidates)
        except Exception as e:                      # résilience §22
            summary["errors"].append(
                {"kind": "cycle", "code": type(e).__name__})
        finished_at = now_iso()
        dur_ms = int((time.monotonic() - t0) * 1000)
        summary["finished_at"] = finished_at
        summary["duration_ms"] = dur_ms
        # cache hits du cycle (journal persistant)
        try:
            hits = sum(1 for e in self.journal.events_since(started_at)
                       if e.get("cache_hit"))
        except Exception:
            hits = 0
        # métriques + alertes §28/§29
        n_matches = max(1, len(summary["matches"]))
        n_err = len(summary["errors"])
        rpm = summary["requests"] / n_matches if summary["matches"] else 0.0
        fail_pct = (100.0 * n_err / max(1, summary["requests"] + n_err))
        ident_total = summary["identity_ok"] + summary["identity_unknown"]
        iunk_pct = (100.0 * summary["identity_unknown"] / ident_total
                    if ident_total else 0.0)
        stats = {"requests_per_match": round(rpm, 2),
                 "source_failures_pct": fail_pct,
                 "identity_unknown_pct": iunk_pct}
        over_hard = rpm > self.config["alerts"]["requests_per_match_error"]
        alerts = _metrics.check_cycle_thresholds(stats, self.config,
                                                 db_module=self.db)
        status = ("DONE_WITH_ERRORS" if (n_err or over_hard or alerts)
                  else "DONE")
        self._cycle_finish(cycle_id, finished_at, len(summary["matches"]),
                           summary["requests"], hits, summary["errors"],
                           dur_ms, status)
        self._state_set(f"last_cycle:{tier or 'all'}", finished_at)
        self.metrics.incr("ingestion_cycles")
        self.metrics.incr("successful_cycles" if status == "DONE"
                          else "failed_cycles")
        if summary["requests"]:
            self.metrics.incr("source_requests", summary["requests"])
        if hits:
            self.metrics.incr("cache_hits", hits)
        if n_err:
            self.metrics.incr("source_errors", n_err)
        self.metrics.incr("cycle_duration_ms_total", dur_ms)
        summary["cache_hits"] = hits
        summary["status"] = status
        summary["alerts"] = [a["code"] for a in alerts]
        return summary

    def _matches_for_tier(self, tier, now):
        rows = self.select_upcoming(now)
        if tier in (None, "calendar"):
            return rows                    # HORIZON + tous (candidats filtrés)
        return [r for r in rows
                if self.phase_for(r["kickoff_time_utc"], now) == tier]

    def _research_one(self, orchestrator, match_row, now, summary,
                      activate_candidates):
        mid = match_row["id"]
        ctx = self.build_ctx(match_row, now)
        now = now or self.clock()
        as_of = now.isoformat()                # horloge du scheduler (tests
                                               # déterministes + cohérence §15)
        try:
            report = orchestrator.research_match(ctx, as_of=as_of)
        except Exception as e:               # §22 — un match ne casse rien
            summary["errors"].append({"match_id": mid,
                                      "code": type(e).__name__})
            return
        summary["requests"] += report.requests_used
        summary["matches"].append(mid)
        for err in report.errors:
            summary["errors"].append({"match_id": mid, **err})
        # Garantie store (idempotente via dedupe_key) : le vrai orchestrateur
        # persiste déjà ; cette ligne est sans effet dans ce cas et rend le
        # pipeline robuste même avec un orchestrateur qui ne persisterait pas.
        try:
            self.store.add_many(report.points)
        except Exception as e:
            summary["errors"].append({"match_id": mid, "kind": "pit_store",
                                      "code": type(e).__name__})
        self._consume_identity(report, match_row)
        if report.identity:
            st = (report.identity or {}).get("status")
            if st in ("MATCH_AUTO", "MATCH_JOURNALIZED"):
                summary["identity_ok"] += 1
            else:
                summary["identity_unknown"] += 1
        # ---- §13 candidats T-180/T-60/T-15 (bridge protégé kickoff §15)
        phase = self.phase_for(match_row["kickoff_time_utc"], now)
        if activate_candidates and phase in self.config["snapshot_windows"]:
            try:
                res = _bridge.create_snapshot_candidate(
                    self.store, mid, match_row["kickoff_time_utc"], phase,
                    as_of=as_of, registry=self.registry,
                    journal=self.journal, metrics=self.metrics,
                    meta={"cycle_trigger": summary["trigger"]},
                    db_module=self.db, now=as_of)
                summary["candidates"].append(
                    {"match_id": mid, "t_window": phase,
                     "created": res.get("created"),
                     "reason": res.get("reason")})
            except Exception as e:
                summary["errors"].append(
                    {"match_id": mid, "kind": "bridge",
                     "code": type(e).__name__})

    def _consume_identity(self, report, match_row):
        """§17 — seeding entity_map UNIQUEMENT par preuves (seuils WEB-1).
        La confiance utilisée est CELLE de la décision d'identité du match :
        < 0.80 ⇒ le store refuse (UNKNOWN), jamais de mapping douteux."""
        if not report.identity:
            return
        conf = float((report.identity or {}).get("confidence") or 0.0)
        if conf < 0.80:
            return
        ev = {"cycle": "scheduler", "identity": report.identity}
        for side in ("home", "away"):
            name = match_row.get(f"{side}_team")
            ext = match_row.get(f"{side}_team_ext_id")
            if name and ext and match_row.get("source") == "espn":
                self.entities.propose(
                    "team", team_key(name), "espn", str(ext),
                    external_name=name, confidence=conf, evidence=ev)

    # ---------------------------------------------------------------- SQL cycles
    def _cycle_insert(self, cycle_id, trigger, tier, started_at):
        self.db.execute(
            """INSERT INTO web_ingestion_cycles
               (cycle_id, trigger, phase, started_at, status)
               VALUES (?,?,?,?,?)""",
            (cycle_id, trigger, tier or "mixed", started_at, "RUNNING"))

    def _cycle_finish(self, cycle_id, finished_at, match_count,
                      request_count, cache_hits, errors, duration_ms, status):
        self.db.execute(
            """UPDATE web_ingestion_cycles
               SET finished_at=%s, match_count=%s, request_count=%s,
                   cache_hits=%s, errors_json=%s, duration_ms=%s, status=%s
               WHERE cycle_id=%s""",
            (finished_at, match_count, request_count, cache_hits,
             json.dumps(errors, ensure_ascii=False, default=str), duration_ms,
             status, cycle_id))

    # ---------------------------------------------------------------- boot §23/§37
    def last_finished_cycle(self):
        r = self.db.query(
            """SELECT * FROM web_ingestion_cycles WHERE finished_at IS NOT NULL
               ORDER BY finished_at DESC LIMIT 1""", one=True)
        return dict(r) if r else None

    def recover_on_boot(self, now=None):
        """Après restart : si le dernier cycle est trop vieux (ou absent),
        lance immédiatement UN cycle RECOVERY (canary : mêmes bornes)."""
        now = now or self.clock()
        last = self.last_finished_cycle()
        stale_sec = (self.config["tick_sec"] *
                     self.config["stale_cycle_factor"])
        need = last is None
        if last and last.get("finished_at"):
            try:
                need = (now - _parse_iso(last["finished_at"])).total_seconds() \
                    >= stale_sec
            except Exception:
                need = True
        if not need:
            return {"recovered": False, "reason": "FRESH"}
        return {"recovered": True,
                "cycle": self.run_cycle(trigger="RECOVERY", tier="calendar",
                                        now=now)}

    # ---------------------------------------------------------------- boucle
    def tick(self, now=None):
        """Un passage de planification : lance les tiers dus (fréquences)."""
        now = now or self.clock()
        ran = []
        for tier in TIERS:
            if tier == "live" and not self.config.get("live_phase_enabled"):
                continue
            if self.due(tier, now):
                summary = self.run_cycle(trigger="SCHEDULED", tier=tier,
                                         now=now)
                ran.append({"tier": tier, "status": summary["status"],
                            "matches": len(summary["matches"]),
                            "requests": summary["requests"]})
        return ran

    def serve_forever(self):
        """Boucle infinie du scheduler (thread daemon §37). Toute exception
        interne est absorbée : un bug d'ingestion NE DOIT PAS tuer l'app."""
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                try:
                    self.metrics.incr("failed_cycles")
                except Exception:
                    pass
                print(f"[web4][scheduler] {type(e).__name__}: {e}")
            self._stop.wait(float(self.config["tick_sec"]))

    def stop(self):
        self._stop.set()
