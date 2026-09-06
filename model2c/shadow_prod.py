# -*- coding: utf-8 -*-
"""
ÉTAPE 2C.1 — SHADOW PRODUCTION RUNNER (EXPERIMENTAL)
=====================================================
Branche le moteur 2C (B/C/D validés hors échantillon) sur les données RÉELLES
produites par WEB-4 — EN SHADOW UNIQUEMENT.

GARANTIES (mission 2C.1) :
- P0 : AUCUNE écriture dans les tables 2A ; AUCUNE exposition publique ;
- §4  : activation explicite (voir shadow_hook.enabled) — OFF par défaut ;
- §5  : AUCUNE ingestion réseau (seed figé + tables 2A en lecture seule) ;
- §6  : anti-leakage audité par prédiction (effective_at ET retrieved_at ≤ T,
        sinon REFUSE_PREDICTION + alerte CRITICAL) ;
- §13 : labels T-180/T-60/T-15 conservés SÉPARÉMENT (append-only) ;
- §14 : kickoff passé ⇒ KICKOFF_PASSED, jamais de prédiction ex-post ;
- §17 : alertes isolées dans model2c_shadow_alerts (jamais web_alerts) ;
- §18 : timings feature/model/db/total consignés (heartbeat append-only).
"""

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

from .features import TeamHistory, build_match_features
from .guards import data_level, prediction_allowed, DataQuality
from .identity import canonical_team, norm_name
from .models import Model2C_B, Model2C_C, Model2C_D
from .config2c import CONFIG_2C as C, FEATURE_VERSION
from . import shadow_assets as assets

UTC = timezone.utc

# compétitions prod (ESPN) → ligues 2C (mapping WEB-4 scheduler OL_SLUG)
COMP_TO_LEAGUE = {"ger.1": "bl1", "ger.2": "bl2"}

MODEL_B = "MODEL_2C_B"
MODEL_C = "MODEL_2C_C"
MODEL_D = "MODEL_2C_D"
SHADOW_MODEL_VERSION = "2c.1-shadow-prod/1.0"

PHASES = ((15, "T-15"), (60, "T-60"), (180, "T-180"))

IDENTITY_AUTO_THRESHOLD = 0.95      # hérité WEB-1 (AUTO ≥ 0.95)
CODE_HASH_CACHE = {"v": None}


def _utcnow():
    return datetime.now(UTC)


def _iso(dt):
    return dt.astimezone(UTC).isoformat()


def _phase_label(kickoff_dt, now_dt):
    delta_min = (kickoff_dt - now_dt).total_seconds() / 60.0
    for lim, label in PHASES:
        if delta_min <= lim:
            return label
    return None


def _sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=True, default=str).encode()).hexdigest()


def _code_hash():
    if CODE_HASH_CACHE["v"] is None:
        from .registry2c import compute_code_hash
        CODE_HASH_CACHE["v"] = compute_code_hash()
    return CODE_HASH_CACHE["v"]


class ShadowRunner:
    """Une exécution = les matchs éligibles DU MOMENT (canary : ≤5)."""

    def __init__(self, db_module, store=None, max_matches=5, seed=None,
                 calibration="auto", min_matches_guard=True):
        self.db = db_module
        self.store = store                    # PersistentPITStore (lecture seule)
        self.max_matches = int(max_matches)
        self._seed = seed                     # injection test (None = asset réel)
        self._calib = None if calibration == "auto" else calibration
        self.min_matches_guard = min_matches_guard

    # ---------------------------------------------------------------- alertes §17
    def _alert(self, level, code, detail=None):
        try:
            self.db.execute(
                """INSERT INTO model2c_shadow_alerts (level, code, detail_json, at_utc)
                   VALUES (%s,%s,%s,%s)""",
                (level, code, json.dumps(detail or {}, ensure_ascii=False,
                                         default=str), _iso(_utcnow())))
        except Exception as e:  # migration absente / DB verrouillée → print
            print(f"[2c-shadow][alerte-non-persistée] {level} {code}: "
                  f"{type(e).__name__}")

    # ------------------------------------------------------------ état/historique
    def _history(self, now_dt):
        """seed (kickoff < T strict) + événements prod repliés (kickoff < T)."""
        t_iso = _iso(now_dt)
        seed = self._seed if self._seed is not None else assets.seed_matches()
        hist = [m for m in seed if m["kickoff_utc"][:19] < t_iso[:19]]
        max_eff = hist[-1]["kickoff_utc"] if hist else None
        max_ret = (assets.manifest().get("generated_at")
                   if self._seed is None else None)
        for ev in self.db.rows_to_dicts(self.db.query(
                """SELECT match_id, league, kickoff_utc, home, away, hg, ag,
                          captured_at FROM model2c_team_events
                   WHERE kickoff_utc < %s ORDER BY kickoff_utc""", (t_iso,))):
            hist.append({"match_id": ev["match_id"], "league": ev["league"],
                         "season": None, "kickoff_utc": ev["kickoff_utc"],
                         "home": ev["home"], "away": ev["away"],
                         "hg": ev["hg"], "ag": ev["ag"],
                         "effective_at": ev["kickoff_utc"],
                         "source": "prod:2a_results"})
            if max_eff is None or ev["kickoff_utc"] > max_eff:
                max_eff = ev["kickoff_utc"]
            if ev.get("captured_at") and (max_ret is None
                                          or ev["captured_at"] > max_ret):
                max_ret = ev["captured_at"]
        hist.sort(key=lambda m: m["kickoff_utc"])
        return hist, max_eff, max_ret

    def _sync_team_events(self, now_dt):
        """Repli des NOUVEAUX résultats 2A (lecture seule) → model2c_team_events.
        Politique « live » : kickoff < T ET captured_at ≤ T (retrieved_at)."""
        t_iso = _iso(now_dt)
        rows = self.db.rows_to_dicts(self.db.query(
            """SELECT m.id, m.competition, m.kickoff_time_utc, m.home_team,
                      m.away_team, m.home_score, m.away_score, r.captured_at
               FROM matches m JOIN results r ON r.match_id = m.id
               WHERE m.competition IN ('ger.1','ger.2')
                 AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                 AND m.kickoff_time_utc < %s AND r.captured_at <= %s
               ORDER BY m.kickoff_time_utc""", (t_iso, t_iso)))
        known = {r["match_id"] for r in self.db.rows_to_dicts(self.db.query(
            "SELECT match_id FROM model2c_team_events"))}
        inserted = skipped_unknown = 0
        for r in rows:
            if r["id"] in known:
                continue
            lg = COMP_TO_LEAGUE.get(r["competition"])
            hc, ac = canonical_team(r["home_team"]), canonical_team(r["away_team"])
            if not lg or not hc or not ac:
                skipped_unknown += 1
                continue
            self.db.execute(
                """INSERT OR IGNORE INTO model2c_team_events
                   (match_id, league, kickoff_utc, home, away, hg, ag,
                    captured_at, processed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (r["id"], lg, r["kickoff_time_utc"], hc, ac,
                 r["home_score"], r["away_score"], r["captured_at"],
                 t_iso))
            inserted += 1
        return {"new_results": inserted, "results_identity_unknown": skipped_unknown}

    # ---------------------------------------------------------------- DC params
    def _dc_params(self, league, now_dt, hist):
        """Params DC du jour (cache append-only) — refit ≤ 1×/jour/ligue."""
        day = now_dt.date().isoformat()
        row = self.db.query(
            """SELECT params_json FROM model2c_dc_params
               WHERE league=%s AND fit_day=%s LIMIT 1""", (league, day), one=True)
        if row:
            try:
                from .dc import DixonColesParams
                p = json.loads(row["params_json"])
                return DixonColesParams(p["teams"],
                                        [p["attack"][t] for t in p["teams"]],
                                        [p["defense"][t] for t in p["teams"]],
                                        p["home_adv"], p["rho"], p["xi"],
                                        p["n_train"], p["xg_used"])
            except Exception as e:
                self._alert("ERROR", "SHADOW_DC_CACHE_CORRUPT",
                            {"league": league, "err": type(e).__name__})
                return None
        from . import dc as dcm
        hp = assets.frozen_hyperparams()
        train = [m for m in hist if m["league"] == league]
        params = dcm.fit(train, xi=hp["dc_xi"], ref_time=now_dt)
        if params is None:
            self._alert("WARNING", "SHADOW_DC_FIT_EMPTY", {"league": league})
            return None
        payload = {"teams": params.teams, "attack": params.attack,
                   "defense": params.defense, "home_adv": params.home_adv,
                   "rho": params.rho, "xi": params.xi,
                   "n_train": params.n_train, "xg_used": params.xg_used}
        try:
            self.db.execute(
                """INSERT OR IGNORE INTO model2c_dc_params
                   (league, fit_day, params_json, n_train, dataset_hash, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (league, day, json.dumps(payload, sort_keys=True),
                 params.n_train, assets.dataset_hash(), _iso(now_dt)))
        except Exception as e:
            self._alert("WARNING", "SHADOW_DC_CACHE_WRITE_FAILED",
                        {"league": league, "err": type(e).__name__})
        return params

    # ------------------------------------------------------------- éligibilité
    def _eligible(self, now_dt):
        t_iso = _iso(now_dt)
        horizon = _iso(now_dt + timedelta(minutes=180))
        return self.db.rows_to_dicts(self.db.query(
            """SELECT id, source, source_match_id, competition, home_team,
                      away_team, kickoff_time_utc, status
               FROM matches
               WHERE status='UPCOMING' AND competition IN ('ger.1','ger.2')
                 AND kickoff_time_utc > %s AND kickoff_time_utc <= %s
               ORDER BY kickoff_time_utc LIMIT %s""",
            (t_iso, horizon, self.max_matches)))

    def _already_ok(self, match_id, label):
        r = self.db.query(
            """SELECT 1 AS x FROM predictions_2c_shadow
               WHERE match_id=%s AND model_id=%s AND snapshot_label=%s
                 AND status='OK' LIMIT 1""", (match_id, MODEL_B, label), one=True)
        return bool(r)

    # --------------------------------------------------------------- écritures
    def _write_prediction(self, *, match, label, t_iso, model_id, raw, cal,
                          feats_hash, dq, sample_size, identity, audit,
                          calibration_version, status="OK", refusal=None):
        pid = _sha({"m": match["id"], "l": label, "model": model_id,
                    "t": t_iso, "raw": raw})[:32]
        row_hash = _sha({"pid": pid, "raw": raw, "cal": cal, "fh": feats_hash})
        calib = cal if cal else {}
        self.db.execute(
            """INSERT INTO predictions_2c_shadow
               (id, match_id, prediction_time, model_version, model_id,
                raw_probabilities, calibrated_probabilities, features_hash,
                dataset_hash, data_quality_json, sample_size,
                calibration_version, code_hash, status, shadow_hash, created_at,
                snapshot_label, refusal_reason, raw_home, raw_draw, raw_away,
                calibrated_home, calibrated_draw, calibrated_away,
                max_feature_effective_at, max_feature_retrieved_at,
                source_match_id, canonical_match_id, identity_confidence,
                identity_method, data_level, feature_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (pid, match["id"], t_iso, SHADOW_MODEL_VERSION, model_id,
             json.dumps(raw, sort_keys=True) if raw else "{}",
             json.dumps(cal, sort_keys=True) if cal else None,
             feats_hash, assets.dataset_hash(),
             json.dumps(dq or {}, sort_keys=True, ensure_ascii=False),
             int(sample_size or 0), calibration_version, _code_hash(), status,
             row_hash, _iso(_utcnow()), label, refusal,
             (raw or {}).get("1"), (raw or {}).get("N"), (raw or {}).get("2"),
             calib.get("1"), calib.get("N"), calib.get("2"),
             audit.get("max_feature_effective_at"),
             audit.get("max_feature_retrieved_at"),
             match.get("source_match_id"), match["id"],
             identity.get("confidence"), identity.get("method"),
             dq.get("level"), FEATURE_VERSION))
        return pid

    def _write_features(self, match_id, feats, as_of_iso, snapshot_id):
        self.db.execute(
            """INSERT INTO features_2c
               (match_id, team_id, feature_name, feature_value, feature_json,
                as_of, source, snapshot_id, feature_version, feature_hash,
                created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (match_id, None, "__vector__", None,
             json.dumps({k: v for k, v in feats.items() if not k.startswith("_")},
                        sort_keys=True, ensure_ascii=True, default=str),
             as_of_iso, "seed:openligadb+2a_results", snapshot_id,
             FEATURE_VERSION, feats["feature_hash"], _iso(_utcnow())))

    # -------------------------------------------------------------- comparateur
    def compare_with_2a(self, limit_per_label=100000):
        """§15 — READ-ONLY. Jointure shadow OK × 2A (1N2 dernière version ≤
        kickoff) × results. Brier/LogLoss UNIQUEMENT si N ≥ 30 par label."""
        out = {}
        for label, _, _ in ((l, None, None) for l in ("T-180", "T-60", "T-15")):
            rows = self.db.rows_to_dicts(self.db.query(
                """SELECT s.match_id, s.calibrated_home ch, s.calibrated_draw cd,
                          s.calibrated_away ca, s.raw_home rh, s.raw_draw rd,
                          s.raw_away ra, m.home_score hg, m.away_score ag,
                          MAX(p.version_seq) vs
                   FROM predictions_2c_shadow s
                   JOIN matches m ON m.id = s.match_id
                   JOIN results r ON r.match_id = s.match_id
                   JOIN predictions p ON p.match_id = s.match_id
                     AND p.market='1N2'
                   WHERE s.status='OK' AND s.model_id=%s AND s.snapshot_label=%s
                   GROUP BY s.match_id LIMIT %s""",
                (MODEL_B, label, limit_per_label)))
            n = len(rows)
            entry = {"n": n}
            if n < int(C["minimum_sample_size"]):
                entry["verdict"] = "INSUFFICIENT_SAMPLE"
            else:
                import math
                b2c = b2a = l2c = l2a = 0.0
                used = 0
                for r in rows:
                    dist_a = self._dist_2a(r["match_id"], r["vs"])
                    if not dist_a:
                        continue
                    oc = "1" if r["hg"] > r["ag"] else ("N" if r["hg"] == r["ag"] else "2")
                    pc = {"1": r["ch"] or r["rh"], "N": r["cd"] or r["rd"],
                          "2": r["ca"] or r["ra"]}
                    b2c += sum((pc[k] - (oc == k)) ** 2 for k in ("1", "N", "2"))
                    b2a += sum((dist_a[k] - (oc == k)) ** 2 for k in ("1", "N", "2"))
                    l2c += -math.log(max(pc[oc], 1e-12))
                    l2a += -math.log(max(dist_a[oc], 1e-12))
                    used += 1
                if used < int(C["minimum_sample_size"]):
                    entry.update({"n": used, "verdict": "INSUFFICIENT_SAMPLE"})
                else:
                    entry.update({"n": used, "verdict": "OK",
                                  "brier_2c": round(b2c / used, 5),
                                  "brier_2a": round(b2a / used, 5),
                                  "logloss_2c": round(l2c / used, 5),
                                  "logloss_2a": round(l2a / used, 5)})
            out[label] = entry
        return out

    def _dist_2a(self, match_id, version_seq):
        """Distribution 1N2 2A = `distribution_json` de la ligne (match, version,
        market='1N2') — UNE seule ligne par (match, version, marché)."""
        r = self.db.query(
            """SELECT distribution_json FROM predictions
               WHERE match_id=%s AND market='1N2' AND version_seq=%s LIMIT 1""",
            (match_id, version_seq), one=True)
        if not r or not r["distribution_json"]:
            return None
        try:
            d = json.loads(r["distribution_json"])
        except Exception:
            return None
        draw = d.get("N", d.get("X"))
        try:
            dist = {"1": float(d["1"]), "N": float(draw), "2": float(d["2"])}
        except (TypeError, KeyError):
            return None
        tot = sum(dist.values())
        if tot <= 0:
            return None
        return {k: v / tot for k, v in dist.items()}

    # ------------------------------------------------------------------- RUN
    def run_once(self, now=None, cycle_id=None):
        """Un passage shadow. NE LÈVE JAMAIS d'exception métier vers l'appelant
        (les erreurs match sont isolées ; les erreurs fatales → résumé error)."""
        t0 = time.monotonic()
        now_dt = now if isinstance(now, datetime) else _utcnow()
        t_iso = _iso(now_dt)
        timings = {"feature_ms": 0, "model_ms": 0, "db_ms": 0, "total_ms": 0}
        summary = {"at": t_iso, "cycle_id": cycle_id, "eligible": 0,
                   "predictions": 0, "skipped_dedup": 0, "refusals": {},
                   "errors": [], "alerts": [], "comparator": None}

        # ---- garde-fous assets (§17 : modèle/calibration absents, hash) ----
        if self._seed is None:
            if not assets.available():
                self._alert("CRITICAL", "SHADOW_ASSETS_MISSING", {})
                summary["errors"].append("SHADOW_ASSETS_MISSING")
                return self._finish(summary, timings, t0, cycle_id)
            ok_h, problems = assets.verify_hashes()
            if not ok_h:
                self._alert("CRITICAL", "SHADOW_HASH_MISMATCH", problems)
                summary["errors"].append("SHADOW_HASH_MISMATCH")
                return self._finish(summary, timings, t0, cycle_id)
        calib = self._calib if self._calib is not None else assets.load_calibration()
        calibration_version = (calib or {}).get("calibration_version") or "MISSING"
        if calib is None:
            self._alert("ERROR", "SHADOW_CALIBRATION_MISSING", {})
            summary["alerts"].append("SHADOW_CALIBRATION_MISSING")

        # ---- §11 : référencement des versions (idempotent, EXPERIMENTAL) ----
        try:
            from .registry2c import register_model
            for mid in (MODEL_B, MODEL_C, MODEL_D):
                register_model(
                    mid, SHADOW_MODEL_VERSION,
                    {"scope": "shadow production 2C.1 — jamais public"},
                    {"leagues": ["bl1", "bl2"],
                     "training": "seed OL 2016-2025 + résultats prod 2026/27"},
                    assets.dataset_hash(), _code_hash(),
                    features={"feature_version": FEATURE_VERSION},
                    status="EXPERIMENTAL",
                    calibration_version=calibration_version)
        except Exception as e:
            self._alert("WARNING", "SHADOW_REGISTRY_WRITE_FAILED",
                        {"err": type(e).__name__})

        # ---- repli des résultats + historique ≤ T ----------------------------
        try:
            sync = self._sync_team_events(now_dt)
            summary["team_events"] = sync
        except Exception as e:
            summary["errors"].append(f"sync:{type(e).__name__}")
            self._alert("ERROR", "SHADOW_SYNC_FAILED", {"err": type(e).__name__})
            return self._finish(summary, timings, t0, cycle_id)

        tf = time.monotonic()
        hist, max_eff, max_ret = self._history(now_dt)
        from .elo import TemporalElo
        from .strengths import OnlineStrengths
        from collections import defaultdict
        hp = assets.frozen_hyperparams()
        elo = TemporalElo(k=hp["elo_k"])
        strengths = defaultdict(OnlineStrengths)
        histories = {}
        for m in hist:
            h, a, lg = m["home"], m["away"], m["league"]
            ko = datetime.fromisoformat(m["kickoff_utc"].replace("Z", "+00:00"))
            elo.apply_result(h, a, m["hg"], m["ag"])
            strengths[lg].update(h, a, m["hg"], m["ag"], m["kickoff_utc"],
                                 m["kickoff_utc"])
            histories.setdefault(h, TeamHistory(h)).add(ko, m["hg"], m["ag"], "H", a)
            histories.setdefault(a, TeamHistory(a)).add(ko, m["ag"], m["hg"], "A", h)
        timings["feature_ms"] += int((time.monotonic() - tf) * 1000)

        audit = {"max_feature_effective_at": max_eff,
                 "max_feature_retrieved_at": max_ret}

        seed_teams = {m["home"] for m in hist} | {m["away"] for m in hist}

        # ---- boucle matchs éligibles ----------------------------------------
        seed_set = seed_teams
        for match in self._eligible(now_dt):
            summary["eligible"] += 1
            try:
                self._predict_one(match, now_dt, t_iso, label_ctx=None,
                                  elo=elo, strengths=strengths,
                                  histories=histories, seed_set=seed_set,
                                  calib=calib, calibration_version=calibration_version,
                                  audit=audit, hist=hist, summary=summary,
                                  timings=timings)
            except Exception as e:
                summary["errors"].append(
                    {"match_id": match.get("id"), "code": type(e).__name__})
                self._alert("ERROR", "SHADOW_MATCH_EXCEPTION",
                            {"match_id": match.get("id"),
                             "err": f"{type(e).__name__}: {e}"})

        # ---- comparateur (read-only, budget borné) ---------------------------
        try:
            summary["comparator"] = self.compare_with_2a()
        except Exception as e:
            summary["comparator"] = {"error": type(e).__name__}

        return self._finish(summary, timings, t0, cycle_id)

    # ------------------------------------------------------------ un match §9
    def _predict_one(self, match, now_dt, t_iso, label_ctx, *, elo, strengths,
                     histories, seed_set, calib, calibration_version, audit,
                     hist, summary, timings):
        from .availability import parse_ts
        ko = parse_ts(match["kickoff_time_utc"])
        # §14 — jamais de prédiction pré-match après le coup d'envoi
        if ko <= now_dt:
            self._refuse(match, None, t_iso, "KICKOFF_PASSED", {},
                         {"level": None}, audit, summary, critical=True)
            return
        label = _phase_label(ko, now_dt)
        if label is None:
            return                                  # hors fenêtre T-180
        if self._already_ok(match["id"], label):
            summary["skipped_dedup"] += 1
            return                                  # §13 — jamais remplacé

        # ---- §8 identité -----------------------------------------------------
        league = COMP_TO_LEAGUE.get(match["competition"])
        hc, ac = canonical_team(match["home_team"]), canonical_team(match["away_team"])
        id_ok = (league and hc and ac and hc in seed_set and ac in seed_set)
        conf = method = None
        if hc and ac:
            exact_h = norm_name(match["home_team"]) == hc
            exact_a = norm_name(match["away_team"]) == ac
            conf = 1.0 if (exact_h and exact_a) else 0.96
            method = "EXACT" if conf == 1.0 else "ALIAS_DECLARED"
        identity = {"home_canonical": hc, "away_canonical": ac,
                    "confidence": conf, "method": method, "league": league}
        if (not id_ok) or conf is None or conf < IDENTITY_AUTO_THRESHOLD:
            if not (hc and ac):
                reason = "IDENTITY_UNKNOWN:UNMAPPED_NAME"
            elif not (hc in seed_set and ac in seed_set):
                reason = "IDENTITY_UNKNOWN:NO_SEED_HISTORY"
            else:
                reason = "IDENTITY_UNKNOWN:LOW_CONFIDENCE"
            self._refuse(match, label, t_iso, reason, identity,
                         {"level": None}, audit, summary)
            return

        # ---- audit PIT (lecture seule — traçabilité §2/§6) -------------------
        pit_usable = 0
        if self.store is not None:
            try:
                pit_usable = len(self.store.query(
                    match_id=match["id"], as_of=t_iso, only_valid=False))
            except Exception:
                pit_usable = -1

        # ---- §6 anti-leak : l'audit DOIT tenir avant toute prédiction --------
        if (audit.get("max_feature_effective_at")
                and audit["max_feature_effective_at"][:19] > t_iso[:19]):
            self._refuse(match, label, t_iso, "REFUSE_PREDICTION:LEAK_EFFECTIVE",
                         identity, {"level": None}, audit, summary,
                         critical=True)
            return
        if (audit.get("max_feature_retrieved_at")
                and audit["max_feature_retrieved_at"][:19] > t_iso[:19]):
            self._refuse(match, label, t_iso, "REFUSE_PREDICTION:LEAK_RETRIEVED",
                         identity, {"level": None}, audit, summary,
                         critical=True)
            return

        # ---- features (même sémantique que le backtest validé) ----------------
        tf = time.monotonic()
        elo_pre = elo.pre_match(hc, ac)
        sfeats = strengths[league].features_pre(
            hc, ac, t_iso, elo_home=elo_pre["elo_home"],
            elo_away=elo_pre["elo_away"], xg_available=False)
        m4f = {"match_id": match["id"], "league": league, "season": None,
               "kickoff_utc": match["kickoff_time_utc"], "home": hc, "away": ac,
               "hg": None, "ag": None}
        feats = build_match_features(
            m4f, t_iso, elo_pre, sfeats,
            histories.setdefault(hc, TeamHistory(hc)),
            histories.setdefault(ac, TeamHistory(ac)))
        timings["feature_ms"] += int((time.monotonic() - tf) * 1000)

        lvl = data_level(sfeats.get("home_matches") or 0,
                         sfeats.get("away_matches") or 0)
        dq = DataQuality(lvl, sfeats.get("home_matches") or 0,
                         sfeats.get("away_matches") or 0, False,
                         ["seed:openligadb", "2a_results"]).as_dict()
        dq["pit_points_usable"] = pit_usable
        sample_size = (sfeats.get("home_matches") or 0) + (sfeats.get("away_matches") or 0)

        if not prediction_allowed(lvl):
            self._refuse(match, label, t_iso, "INSUFFICIENT_DATA", identity,
                         dq, audit, summary)
            return

        # ---- modèles ----------------------------------------------------------
        tm = time.monotonic()
        pred_b = Model2C_B().predict(sfeats)
        dc_params = self._dc_params(league, now_dt, hist)
        pred_c = Model2C_C(dc_params).predict(hc, ac) if dc_params else None
        weight_c = assets.frozen_hyperparams()["weight_c"]
        pred_d = (Model2C_D(weight_c).predict(pred_b, pred_c, hc, ac)
                  if pred_c else None)
        timings["model_ms"] += int((time.monotonic() - tm) * 1000)

        td = time.monotonic()
        written = 0
        for model_id, pred in ((MODEL_B, pred_b), (MODEL_C, pred_c),
                               (MODEL_D, pred_d)):
            if pred is None:
                if model_id in (MODEL_C, MODEL_D):
                    summary["alerts"].append(f"SHADOW_MODEL_UNAVAILABLE:{model_id}")
                continue
            raw = {k: round(pred["markets"][k], 6) for k in ("1", "N", "2")}
            cal = None
            mc = (calib or {}).get("models", {}).get(model_id)
            if mc and mc.trained:
                cd = mc.predict(raw)
                cal = {k: round(v, 6) for k, v in cd.items()}
            self._write_prediction(
                match=match, label=label, t_iso=t_iso, model_id=model_id,
                raw=raw, cal=cal, feats_hash=feats["feature_hash"], dq=dq,
                sample_size=sample_size, identity=identity, audit=audit,
                calibration_version=calibration_version)
            written += 1
        self._write_features(match["id"], feats, t_iso, None)
        timings["db_ms"] += int((time.monotonic() - td) * 1000)
        summary["predictions"] += written

    def _refuse(self, match, label, t_iso, reason, identity, dq, audit,
                summary, critical=False):
        """NO_PREDICTION tracée (§23 2C : aucune prédiction > fausse précision)."""
        self._write_prediction(
            match={**match, "source_match_id": match.get("source_match_id")},
            label=label or "REFUSED", t_iso=t_iso, model_id="MODEL_2C_SHADOW",
            raw=None, cal=None, feats_hash=None, dq=dq, sample_size=0,
            identity=identity or {}, audit=audit,
            calibration_version="n/a", status="NO_PREDICTION", refusal=reason)
        summary["refusals"][reason.split(":")[0]] = \
            summary["refusals"].get(reason.split(":")[0], 0) + 1
        if critical:
            self._alert("CRITICAL", reason,
                        {"match_id": match.get("id"), "label": label})

    def _finish(self, summary, timings, t0, cycle_id):
        timings["total_ms"] = int((time.monotonic() - t0) * 1000)
        summary["timings"] = timings
        try:
            self.db.execute(
                """INSERT INTO model2c_shadow_heartbeats
                   (cycle_id, at_utc, duration_ms, timings_json, summary_json)
                   VALUES (%s,%s,%s,%s,%s)""",
                (cycle_id, summary.get("at"), timings["total_ms"],
                 json.dumps(timings, sort_keys=True),
                 json.dumps(summary, ensure_ascii=False, default=str)))
        except Exception as e:
            summary["errors"].append(f"heartbeat:{type(e).__name__}")
        return summary
