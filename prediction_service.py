# -*- coding: utf-8 -*-
"""
SERVICE DES PRÉDICTIONS (ÉTAPE 2A — le cœur du socle de vérité)
================================================================
Règles ABSOLUES implémentées ici :

§6/§7  Une prédiction publiée est GELÉE immédiatement (append-only) ; chaque
       nouvelle information (compositions, cotes, forme) crée une NOUVELLE
       VERSION. Les anciennes versions ne sont jamais écrasées.
§9     ANTI-FUITE : publish_match() REFUSE toute création de prédiction une
       fois le coup d'envoi passé (now >= kickoff). Les features du snapshot
       ne contiennent que des données d'avant-match.
§10    Le règlement (settle) n'invente JAMAIS de prédiction historique : un
       match terminé sans version gelée pré-match devient NOT_EVALUABLE.
§13    La distribution 1X2 complète (1/N/2) est conservée pour Brier/LogLoss.
§15    Hash SHA-256 du snapshot + de chaque prédiction (détection de
       modification ultérieure — verify_prediction()).
§16    Chaque ligne porte (model_name, model_version) — une prédiction reste
       liée au modèle qui l'a produite.
§28    Probabilités BRUTES non calibrées : published_probability = raw pour
       l'instant (la calibration arrive à l'ÉTAPE 2C). Les métriques publiques
       ne sont affichées qu'avec un échantillon propre suffisant.
"""

import json
import threading
from datetime import datetime, timezone, timedelta

import db
import repository as repo
import engine

MODEL_NAME = engine.MODEL_NAME
MODEL_VERSION = engine.MODEL_VERSION
MIN_CLEAN_SAMPLE = 20        # §12 : pas de métrique publique en dessous
_PERF_CACHE = {"at": 0.0, "data": None}
_HIST_CACHE = {"at": 0.0, "data": None}

_DISP = {}            # mid -> (ts, display)  affichage gelé (live/terminé)
_PUB = {}             # mid -> (snapshot_hash, seq, display)  dernier publié (pré)
_T15 = set()          # mid ayant reçu l'événement de référence T-15
_DONE = set()         # mid réglés/void/non-évaluables (ne plus re-traiter)
_MEM = threading.Lock()

_TTL_DISP = 120.0


# ---------------------------------------------------------------------------
# Utilitaires temps
# ---------------------------------------------------------------------------
def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(s):
    return datetime.fromisoformat((s or "").replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# SNAPSHOT (§5) — « quelles informations le modèle connaissait-il exactement ? »
# ATTENTION : le payload ne contient AUCUN timestamp → son hash sert aussi à
# détecter tout changement réel des données d'entrée (nouvelle version §8).
# ---------------------------------------------------------------------------
def _team_slice(t):
    if not t:
        return None
    return {"sw": round(t["sw"], 4), "n": t["n"],
            "gf": round(t["gf"], 4), "ga": round(t["ga"], 4),
            "home_sw": round(t["home_sw"], 4), "home_gf": round(t["home_gf"], 4), "home_ga": round(t["home_ga"], 4),
            "away_sw": round(t["away_sw"], 4), "away_gf": round(t["away_gf"], 4), "away_ga": round(t["away_ga"], 4),
            "form": t["form"][:6]}


def build_snapshot_payload(code, ev, stats, detail, pred, absences, odds_probs, odds_dec):
    home_t = _team_slice(stats["teams"].get(ev["home"]["id"]))
    away_t = _team_slice(stats["teams"].get(ev["away"]["id"]))
    fields = ["team_stats", "league_avgs"]
    if absences != (0, 0):
        fields.append("injuries")
    if detail and detail.get("lineups"):
        fields.append("lineups")
    if odds_probs:
        fields.append("odds")
    reliab = pred.get("reliab", 0)
    return {
        "engine": {"name": MODEL_NAME, "version": MODEL_VERSION,
                   "config_hash": repo.sha256_text(repo.canonical_json(engine.MODEL_CONFIG))[:16]},
        "features": {
            "league": {"homeAvg": stats["league"]["homeAvg"], "awayAvg": stats["league"]["awayAvg"],
                       "n": stats["league"]["n"]},
            "home": home_t, "away": away_t,
            "absences": list(absences),
            "odds_probs": list(odds_probs) if odds_probs else None,
            "odds_dec": odds_dec,
            "lineups_present": bool(detail and detail.get("lineups")),
        },
        "output": pred,
    }, fields, ("full" if (reliab or 0) >= 0.6 else "partial")


# ---------------------------------------------------------------------------
# PUBLICATION / GEL (§6/§7/§8)
# ---------------------------------------------------------------------------
def publish_match(code, ev, stats, detail=None, now=None):
    """Crée (gèle) une version de prédiction pour un match À VENIR.

    Retourne l'objet d'affichage frontend, ou None si :
    - les stats d'équipes sont insuffisantes (DATA_INSUFFICIENT, honnête) ;
    - le coup d'envoi est passé → INTERDIT (anti-fuite absolue, §9).
    """
    now = now or now_utc()
    kickoff = parse_iso(ev["utc"])
    if now >= kickoff:
        return None  # jamais de prédiction après le coup d'envoi — RÈGLE §9

    absences = (0, 0)
    odds_probs = odds_dec = None
    if detail:
        inj = detail.get("injuries") or {}
        absences = (len(inj.get("home", [])), len(inj.get("away", [])))
        odds_probs = detail.get("odds_probs")
        odds_dec = detail.get("odds_dec")

    pred = engine.predict_match(stats, ev["home"]["id"], ev["away"]["id"],
                                absences=absences, odds=odds_probs)
    if pred is None:
        return None

    mid = f"espn:{code}:{ev['id']}"
    payload, fields, quality = build_snapshot_payload(
        code, ev, stats, detail, pred, absences, odds_probs, odds_dec)
    payload_json = repo.canonical_json(payload)
    payload_hash = repo.sha256_text(payload_json)

    with _MEM:
        last = _PUB.get(mid)
        if last and last[0] == payload_hash:
            return last[2]  # rien n'a changé → pas de nouvelle version

    if repo.has_snapshot_hash(mid, payload_hash):
        # Ce jeu exact de données a déjà été gelé (y compris après redémarrage
        # ou oscillation d'une donnée instable) → on RÉUTILISE la version
        # existante, jamais de doublon ni de version ping-pong (§7/TEST 7).
        seq = repo.latest_version_seq(mid)
        rows = repo.predictions_for_version(mid, seq)
        last_snap = rows[0]["snapshot_hash"] if rows else None
        if last_snap != payload_hash:
            # la dernière version gelée correspond à un AUTRE jeu de données ;
            # on relit la version qui correspond vraiment à ce payload
            cand = db.query("""SELECT version_seq, frozen_at FROM predictions
                               WHERE match_id=%s AND snapshot_hash=%s
                               ORDER BY version_seq DESC LIMIT 1""", (mid, payload_hash), one=True)
            if cand:
                rows = repo.predictions_for_version(mid, cand["version_seq"])
                seq = cand["version_seq"]
        disp = _make_display(pred, seq, rows[0]["frozen_at"] if rows else None, ev)
        with _MEM:
            _PUB[mid] = (payload_hash, seq, disp)
        return disp

    # ---- NOUVELLE VERSION GELÉE (append-only) ----
    snap = repo.insert_snapshot(mid, now.isoformat(), "espn", payload, quality, fields)
    seq = repo.latest_version_seq(mid) + 1
    frozen_at = now.isoformat()
    dists = engine.family_distributions(pred)
    bm_odds_1x2 = None
    if odds_dec:
        pick_side = max(dists["1N2"].items(), key=lambda kv: kv[1])[0]
        bm_odds_1x2 = (odds_dec.get(pick_side) or None)

    rows = []
    for fam, dist in dists.items():
        sel = engine.family_pick(fam, dist)
        p_sel = min(max(dist[sel], 1e-9), 1.0)
        fair = round(1.0 / p_sel, 3)
        pid = repo.uid()
        rp6 = round(dist[sel], 6)   # la valeur hachée est EXACTEMENT celle stockée (§15)
        ph = repo.sha256_text(repo.canonical_json({
            "match_id": mid, "version_seq": seq, "market": fam, "selection": sel,
            "raw_probability": rp6, "published_probability": rp6,
            "model": [MODEL_NAME, MODEL_VERSION], "snapshot_hash": snap["hash"],
            "frozen_at": frozen_at}))
        rows.append({"id": pid, "version_seq": seq, "kickoff_time_utc": ev["utc"],
                     "market": fam, "selection": sel,
                     "raw_probability": rp6, "published_probability": rp6,
                     "fair_odds": fair,
                     "bookmaker_odds": bm_odds_1x2 if fam == "1N2" else None,
                     "distribution_json": json.dumps({k: round(v, 6) for k, v in dist.items()}),
                     "model_name": MODEL_NAME, "model_version": MODEL_VERSION,
                     "snapshot_id": snap["id"], "snapshot_hash": snap["hash"], "prediction_hash": ph,
                     "frozen_at": frozen_at})
    repo.insert_prediction_version(mid, rows)
    repo.log_event("prediction_frozen", match_id=mid, detail={
        "version_seq": seq, "snapshot_hash": snap["hash"],
        "replaces": seq - 1 if seq > 1 else None,
        "available_fields": fields, "data_quality": quality})

    if odds_dec:
        repo.insert_odds(mid, now.isoformat(), "espn", [
            {"bookmaker": (detail or {}).get("odds_provider"), "market": "1X2",
             "selection": side, "odds": odd}
            for side, odd in odds_dec.items() if odd])

    disp = _make_display(pred, seq, frozen_at, ev)
    with _MEM:
        _PUB[mid] = (payload_hash, seq, disp)
        _DISP.pop(mid, None)
    return disp


def _make_display(pred, seq, frozen_at, ev):
    d = dict(pred)
    d["frozen"] = True
    d["frozenAt"] = frozen_at
    d["v"] = seq
    d["model"] = {"name": MODEL_NAME, "version": MODEL_VERSION}
    d["calibrated"] = False
    return d


def ensure_t15_reference(code, ev, now=None):
    """§7 — Marque dans le JOURNAL la version DE RÉFÉRENCE à T-15 min : celle
    qui est ACTUELLEMENT en vigueur/affichée à cet instant (pas simplement la
    plus récente — important en cas de donnée oscillante)."""
    now = now or now_utc()
    kickoff = parse_iso(ev["utc"])
    if kickoff - now > timedelta(minutes=15):
        return
    mid = f"espn:{code}:{ev['id']}"
    with _MEM:
        if mid in _T15:
            return
    if repo.has_event(mid, "t15_reference"):
        with _MEM:
            _T15.add(mid)
        return
    with _MEM:
        cur = _PUB.get(mid)
    seq = (cur[1] if cur else None) or repo.latest_version_seq(mid)
    if seq:
        repo.log_event("t15_reference", match_id=mid,
                       detail={"version_seq": seq, "rule": "version en vigueur à T-15 min"})
    with _MEM:
        _T15.add(mid)


def evaluation_seq(mid, kickoff_utc):
    """Version retenue pour l'évaluation (§7) :
    1. la version marquée « référence T-15 » si elle existe ;
    2. sinon la plus récente gelée avant le coup d'envoi.
    Ne retourne JAMAIS une version gelée après le coup d'envoi."""
    ev_row = db.query("""SELECT detail_json FROM prediction_events
                         WHERE match_id=%s AND event='t15_reference' LIMIT 1""", (mid,), one=True)
    if ev_row and ev_row["detail_json"]:
        try:
            seq = json.loads(ev_row["detail_json"]).get("version_seq")
            if seq:
                chk = db.query("""SELECT 1 AS x FROM predictions WHERE match_id=%s AND version_seq=%s
                                  AND frozen_at <= %s LIMIT 1""", (mid, seq, kickoff_utc), one=True)
                if chk:
                    return seq
        except Exception:
            pass
    return repo.evaluation_version(mid, kickoff_utc)


# ---------------------------------------------------------------------------
# AFFICHAGE LIVE / TERMINÉ (depuis la base — JAMAIS de recalcul, §10/§11)
# ---------------------------------------------------------------------------
def frozen_display(code, ev, with_verdicts=False):
    """Affichage d'un match en direct ou terminé : la prédiction GELÉE pré-match
    telle qu'archivée (+ verdict si réglée). Retourne None si aucune n'existe."""
    mid = f"espn:{code}:{ev['id']}"
    now_ts = now_utc().timestamp()
    with _MEM:
        hit = _DISP.get(mid)
        if hit and now_ts - hit[0] < _TTL_DISP:
            return hit[1]
    seq = evaluation_seq(mid, ev["utc"])
    if not seq:
        with _MEM:
            _DISP[mid] = (now_ts, None)
        return None
    rows = repo.predictions_for_version(mid, seq)
    if not rows:
        return None
    snap = repo.get_snapshot(rows[0]["snapshot_id"])
    disp = None
    if snap:
        try:
            out = json.loads(snap["payload_json"])["output"]
            disp = _make_display(out, seq, rows[0]["frozen_at"], ev)
        except Exception:
            disp = None
    if with_verdicts:
        verdicts = _build_verdicts(rows, mid)
        if verdicts:
            disp = disp or {"frozen": True, "v": seq, "frozenAt": rows[0]["frozen_at"],
                            "model": {"name": MODEL_NAME, "version": MODEL_VERSION}}
            disp["verdicts"] = verdicts
    with _MEM:
        _DISP[mid] = (now_ts, disp)
    return disp


def _build_verdicts(rows, mid):
    res = repo.get_result(mid)
    if not res or res["home_score"] is None or res["away_score"] is None:
        return None
    hg, ag = res["home_score"], res["away_score"]
    real = "1" if hg > ag else ("N" if hg == ag else "2")
    fam = {r["market"]: r for r in rows}
    r1 = fam.get("1N2")
    pick = r1["selection"] if r1 else None
    SafeWon = None
    # Le « pari le plus sûr » affiché = sélection la plus probable des 6 familles
    best = max(rows, key=lambda r: r["published_probability"]) if rows else None
    if best and best.get("won") is not None:
        SafeWon = bool(best["won"])
    if best and best.get("won") is None:
        pr = db.query("SELECT won FROM prediction_results WHERE prediction_id=%s", (best["id"],), one=True)
        SafeWon = bool(pr["won"]) if pr and pr["won"] is not None else None
    return {"real": real, "score": f"{hg}-{ag}", "pick": pick,
            "pickOk": (pick == real) if pick else None,
            "safeLabel": f"{best['market']} · {best['selection']}" if best else None,
            "safeP": round(best["published_probability"] * 100, 1) if best else None,
            "safeOk": SafeWon,
            "frozenAt": rows[0]["frozen_at"], "v": rows[0]["version_seq"]}


# ---------------------------------------------------------------------------
# RÈGLEMENT (§10/§11) — une seule fois, à la détection du coup de sifflet final
# ---------------------------------------------------------------------------
def settle_if_finished(code, ev):
    """Règle un match terminé. Anti-fabrication totale :
    - pas de version gelée pré-match → NOT_EVALUABLE (§11), rien d'autre ;
    - sinon → INSERT result + évaluation des 6 familles (Brier/LogLoss).
    Jamais de recalcul du modèle avec les données d'après-match."""
    if ev["state"] != "post":
        return None
    mid = f"espn:{code}:{ev['id']}"
    with _MEM:
        if mid in _DONE:
            return {"skipped": True}
    match = repo.get_match(mid)
    prev_eval = (match or {}).get("evaluation_status")
    if prev_eval in ("SETTLED", "NOT_EVALUABLE", "VOID"):
        with _MEM:
            _DONE.add(mid)
        return {"skipped": True, "status": prev_eval}

    detail = (ev.get("detail") or "").lower()
    cancelled = any(w in detail for w in ("postpon", "cancel", "abandon", "report", "annul"))
    hg = None if ev["home"]["score"] is None else int(float(ev["home"]["score"]))
    ag = None if ev["away"]["score"] is None else int(float(ev["away"]["score"]))
    finished = bool(ev.get("completed")) and hg is not None and ag is not None and not cancelled
    if not finished and not cancelled:
        return None  # pas encore de résultat officiel exploitable

    now = db.utcnow()
    if finished:
        repo.insert_result(mid, hg, ag, ev["home"].get("ht"), ev["away"].get("ht"),
                           (ev.get("detail") or "FT"), ev["utc"], "espn")
        repo.log_event("result_captured", match_id=mid, detail={"score": f"{hg}-{ag}", "at": now})

    if cancelled:
        _void_match(mid)
        with _MEM:
            _DONE.add(mid)
        return {"status": "VOID"}

    seq = evaluation_seq(mid, ev["utc"])
    if not seq:
        # §11 — AUCUNE prédiction pré-match archivée : le match n'entre PAS
        # dans le calcul de précision. On n'invente rien.
        repo.set_evaluation_status(mid, "NOT_EVALUABLE")
        repo.log_event("not_evaluable", match_id=mid,
                       detail={"reason": "aucune prédiction pré-match gelée avant le coup d'envoi"})
        with _MEM:
            _DONE.add(mid)
        return {"status": "NOT_EVALUABLE"}

    rows = repo.predictions_for_version(mid, seq)
    n_set = 0
    for r in rows:
        if db.query("SELECT 1 AS x FROM prediction_results WHERE prediction_id=%s", (r["id"],), one=True):
            continue
        dist = json.loads(r["distribution_json"])
        outcome, won, brier, ll = engine.settle_family(r["market"], r["selection"], dist, hg, ag)
        if outcome is None:
            continue
        repo.insert_prediction_result(r["id"], outcome, 1 if won else 0, now, round(brier, 6), round(ll, 6))
        repo.mark_prediction_status(r["id"], "SETTLED")
        n_set += 1
    repo.set_evaluation_status(mid, "SETTLED")
    repo.log_event("settled", match_id=mid,
                   detail={"version_seq": seq, "markets_scored": n_set, "score": f"{hg}-{ag}"})
    with _MEM:
        _DONE.add(mid)
        _DISP.pop(mid, None)
    _PERF_CACHE["at"] = 0.0
    return {"status": "SETTLED", "markets_scored": n_set}


def _void_match(mid):
    """Match annulé/reporté : prédictions VOID, exclues des métriques (§22)."""
    seq = repo.latest_version_seq(mid)
    if seq:
        for r in repo.predictions_for_version(mid, seq):
            if r["prediction_status"] == "PREDICTION_FROZEN":
                repo.mark_prediction_status(r["id"], "VOID")
    repo.set_evaluation_status(mid, "VOID")
    repo.log_event("void", match_id=mid, detail={"reason": "match annulé/reporté/abandonné"})


# ---------------------------------------------------------------------------
# MÉTRIQUES PROPRES (§12/§13) — uniquement sur versions gelées de référence
# ---------------------------------------------------------------------------
def _perf_key():
    return int(now_utc().timestamp() // 60)


def public_performance(force=False):
    """Agrégats publics. Tant que l'échantillon propre est insuffisant :
    statut « rebuilding » — AUCUN pourcentage trompeur (§12/§26)."""
    if not force and _PERF_CACHE["data"] and _PERF_CACHE["at"] == _perf_key():
        return _PERF_CACHE["data"]
    rows = repo.evaluation_rows_latest()
    settled = [r for r in rows if r.get("won") is not None and r["prediction_status"] == "SETTLED"]
    settled_matches = len({r["match_id"] for r in settled})
    frozen_versions = len({(r["match_id"], r["version_seq"]) for r in rows})
    void_n = len([r for r in rows if r["prediction_status"] == "VOID"])

    if settled_matches < MIN_CLEAN_SAMPLE:
        data = {"status": "rebuilding", "minimum": MIN_CLEAN_SAMPLE,
                "settledMatches": settled_matches, "frozenVersions": frozen_versions,
                "note": "Bilan affiché uniquement sur prédictions gelées pré-match (anti-fuite)."}
        _PERF_CACHE.update(at=_perf_key(), data=data)
        return data

    fam = {}
    for r in settled:
        f = fam.setdefault(r["market"], {"n": 0, "won": 0, "brier": 0.0, "ll": 0.0})
        f["n"] += 1
        f["won"] += r["won"]
        f["brier"] += r["brier_score"] or 0.0
        f["ll"] += r["log_loss"] or 0.0
    by_market = {k: {"n": v["n"], "accuracyPct": round(100 * v["won"] / v["n"], 1),
                     "brier": round(v["brier"] / v["n"], 4), "logLoss": round(v["ll"] / v["n"], 4)}
                 for k, v in sorted(fam.items())}

    fam1 = settled and [r for r in settled if r["market"] == "1N2"] or []
    # « pari le plus sûr » affiché = sélection la plus probable de chaque version
    best_rows = {}
    for r in settled:
        cur = best_rows.get(r["match_id"])
        if not cur or r["published_probability"] > cur["published_probability"]:
            best_rows[r["match_id"]] = r
    safe_n = len(best_rows)
    safe_won = sum(r["won"] for r in best_rows.values())

    # ROI/Yield/Drawdown : uniquement sur les lignes 1N2 ayant une cote réelle
    # capturée au moment du gel. Sinon None — jamais de ROI simulé.
    roi_rows = [r for r in settled if r["market"] == "1N2" and r.get("bookmaker_odds")]
    roi = None
    if len(roi_rows) >= 5:
        stake, profit = 0.0, 0.0
        equity, peak, max_dd = 0.0, 0.0, 0.0
        for r in sorted(roi_rows, key=lambda x: x["frozen_at"]):
            stake += 1.0
            pnl = (r["bookmaker_odds"] - 1.0) if r["won"] else -1.0
            profit += pnl
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        roi = {"n": len(roi_rows), "roiPct": round(100 * profit / stake, 1),
               "yieldPct": round(100 * profit / stake, 1), "profit": round(profit, 2),
               "maxDrawdown": round(max_dd, 2),
               "note": "1 unité par sélection 1N2, cotes réelles capturées au gel."}

    comp = {}
    for r in settled:
        c = comp.setdefault(r["competition"], {"n": 0, "won": 0})
        c["n"] += 1
        c["won"] += r["won"]
    by_comp = sorted([{"competition": k, "n": v["n"], "accuracyPct": round(100 * v["won"] / v["n"], 1)}
                      for k, v in comp.items()], key=lambda x: -x["n"])[:12]
    mv = {}
    for r in settled:
        k = f'{r["model_name"]} {r["model_version"]}'
        c = mv.setdefault(k, {"n": 0, "won": 0, "brier": 0.0})
        c["n"] += 1
        c["won"] += r["won"]
        c["brier"] += r["brier_score"] or 0.0
    by_model = [{"model": k, "n": v["n"], "accuracyPct": round(100 * v["won"] / v["n"], 1),
                 "brier": round(v["brier"] / v["n"], 4)} for k, v in mv.items()]

    n1 = len(fam1)
    data = {"status": "ready", "settledMatches": settled_matches,
            "settledPredictions": len(settled), "frozenVersions": frozen_versions, "voided": void_n,
            "accuracy1N2": round(100 * sum(r["won"] for r in fam1) / n1, 1) if n1 else None,
            "brier1N2": round(sum((r["brier_score"] or 0) for r in fam1) / n1, 4) if n1 else None,
            "logLoss1N2": round(sum((r["log_loss"] or 0) for r in fam1) / n1, 4) if n1 else None,
            "safest": {"n": safe_n, "accuracyPct": round(100 * safe_won / safe_n, 1)} if safe_n else None,
            "roi": roi, "byMarket": by_market, "byCompetition": by_comp, "byModelVersion": by_model,
            "note": "Métriques calculées exclusivement sur prédictions gelées avant coup d'envoi."}
    _PERF_CACHE.update(at=_perf_key(), data=data)
    return data


def calibration_bins():
    """Calibration (préparation §13/ÉTAPE 2C) : probabilité publiée vs
    fréquence observée de gain, par tranches de 10 %, toutes familles."""
    rows = [r for r in repo.evaluation_rows_latest()
            if r.get("won") is not None and r["prediction_status"] == "SETTLED"]
    bins = [{"lo": i * 10, "hi": (i + 1) * 10, "n": 0, "sumP": 0.0, "won": 0} for i in range(10)]
    for r in rows:
        p = min(max(r["published_probability"] or 0.0, 0.0), 0.9999)
        b = bins[int(p * 10)]
        b["n"] += 1
        b["sumP"] += r["published_probability"]
        b["won"] += r["won"]
    if sum(b["n"] for b in bins) < MIN_CLEAN_SAMPLE:
        return {"status": "rebuilding", "minimum": MIN_CLEAN_SAMPLE, "n": sum(b["n"] for b in bins)}
    return {"status": "ready", "bins": [
        {"range": f"{b['lo']}-{b['hi']}%", "n": b["n"],
         "avgPredicted": round(100 * b["sumP"] / b["n"], 1) if b["n"] else None,
         "observedWinRate": round(100 * b["won"] / b["n"], 1) if b["n"] else None}
        for b in bins if b["n"] > 0]}


# ---------------------------------------------------------------------------
# HISTORIQUE PUBLIC (§14/§25)
# ---------------------------------------------------------------------------
def history(limit=50, offset=0, market=None, competition=None, status=None):
    rows = repo.evaluation_rows_latest(limit=600)
    out = []
    for r in rows:
        if market and r["market"] != market:
            continue
        if competition and r["competition"] != competition:
            continue
        st = "SETTLED" if r.get("won") is not None else (
            "VOID" if r["prediction_status"] == "VOID" else "PREDICTION_FROZEN")
        if status and st != status:
            continue
        out.append({"id": r["id"], "matchId": r["match_id"],
                    "match": f"{r['home_team']} — {r['away_team']}", "competition": r["competition"],
                    "market": r["market"], "selection": r["selection"],
                    "publishedProbability": r["published_probability"],
                    "bookmakerOdds": r.get("bookmaker_odds"),
                    "model": f'{r["model_name"]} {r["model_version"]}',
                    "version": r["version_seq"], "frozenAt": r["frozen_at"],
                    "kickoff": r["kickoff_time_utc"], "status": st,
                    "score": r.get("actual_outcome"), "won": r.get("won"),
                    "brier": r.get("brier_score"), "logLoss": r.get("log_loss")})
    total = len(out)
    return {"total": total, "offset": offset, "limit": limit,
            "items": out[offset:offset + limit]}


def match_predictions(mid):
    match = repo.get_match(mid) or repo.get_match_by_source("espn", mid.split(":")[-1])
    if not match:
        return None
    versions = repo.predictions_history(match["id"])
    eval_seq = evaluation_seq(match["id"], match["kickoff_time_utc"])
    timeline = repo.events_for_match(match["id"])
    frozen = {}
    for v in versions:
        frozen.setdefault(v["version_seq"], []).append({
            "id": v["id"], "market": v["market"], "selection": v["selection"],
            "publishedProbability": v["published_probability"],
            "distribution": json.loads(v["distribution_json"]) if v.get("distribution_json") else None,
            "model": f'{v["model_name"]} {v["model_version"]}',
            "frozenAt": v["frozen_at"], "status": v["prediction_status"],
            "hash": v["prediction_hash"][:16]})
        try:
            pr = db.query("SELECT * FROM prediction_results WHERE prediction_id=%s", (v["id"],), one=True)
            if pr:
                frozen[v["version_seq"]][-1]["result"] = dict(pr)
        except Exception:
            pass
    result = repo.get_result(match["id"])
    return {"match": {"id": match["id"], "teams": f"{match['home_team']} — {match['away_team']}",
                      "competition": match["competition"], "kickoff": match["kickoff_time_utc"],
                      "status": match["status"], "evaluationStatus": match["evaluation_status"]},
            "evaluationVersion": eval_seq,
            "versions": [{"version_seq": seq, "markets": mkts} for seq, mkts in sorted(frozen.items())],
            "result": result, "events": timeline}


def prediction_detail(pid):
    p = repo.get_prediction(pid)
    if not p:
        return None
    snap = repo.get_snapshot(p["snapshot_id"])
    ok_hash = verify_prediction(p)
    res = db.query("SELECT * FROM prediction_results WHERE prediction_id=%s", (pid,), one=True)
    return {"prediction": p, "snapshot": snap, "integrity": "OK" if ok_hash else "CORROMPU",
            "result": dict(res) if res else None,
            "events": repo.events_for_prediction(pid)}


def verify_prediction(p):
    """§15 — détecte toute modification inattendue d'une prédiction gelée."""
    expect = repo.sha256_text(repo.canonical_json({
        "match_id": p["match_id"], "version_seq": p["version_seq"], "market": p["market"],
        "selection": p["selection"], "raw_probability": p["raw_probability"],
        "published_probability": p["published_probability"],
        "model": [p["model_name"], p["model_version"]],
        "snapshot_hash": p["snapshot_hash"], "frozen_at": p["frozen_at"]}))
    if expect != p["prediction_hash"]:
        return False
    snap = repo.get_snapshot(p["snapshot_id"])
    if not snap:
        return False
    return repo.sha256_text(snap["payload_json"]) == snap["payload_hash"]


# ---------------------------------------------------------------------------
# Marquage utilisé par les autres modules (feed loop)
# ---------------------------------------------------------------------------
def invalidate_all():
    with _MEM:
        _PUB.clear()
        _DISP.clear()
    _PERF_CACHE["at"] = 0.0
