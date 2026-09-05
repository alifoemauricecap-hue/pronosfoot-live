# -*- coding: utf-8 -*-
"""
PIPELINE 2C — backtest walk-forward complet (§4→§29)
=====================================================
Ordre STRICT des opérations :
1) dataset canonique trié chronologiquement ;
2) sélection des hyperparamètres SUR LES PLIS DE VALIDATION UNIQUEMENT
   (jamais sur le test final) : elo_k, dc_xi, poids ensemble ;
3) passe chronologique unique : features → prédictions (A, B, C, D,
   rejeu 2A) → PUIS mise à jour des états (aucun futur ne passe : P0) ;
4) calibration entraînée sur les plis strictement ANTÉRIEURS (disjointe) ;
5) métriques avec N obligatoire + gardes d'échantillon + bootstrap apparié
   comparant 2A (rejeu) et 2C sur exactement les mêmes matchs ;
6) persistance : model2c_versions / predictions_2c_shadow / features_2c
   (append-only, migration v3 — jamais en production réelle).

Aucun résultat n'est inventé : INSUFFICIENT_SAMPLE / NO_PREDICTION /
NOT_JUSTIFIED restent des réponses légitimes.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone

import db
from .availability import parse_ts
from .config2c import CONFIG_2C as C, FEATURE_VERSION, CALIBRATION_VERSION
from .elo import TemporalElo
from .features import TeamHistory, build_match_features
from .guards import data_level, prediction_allowed, clamp_prob, publish_guard, DataQuality
from .metrics import metric_bundle, ece_binary, reliability_table
from .models import Model2C_A, Model2C_B, Model2C_C, Model2C_D
from .poisson2c import clamp
from . import dc as dcm
from .calibration import MulticlassCalibrator
from .compare import compare as compare_models
from .registry2c import register_model, compute_code_hash, compute_dataset_hash
from .shadow import write_shadow
from .replay2a import OnlineReplay2A
from .strengths import OnlineStrengths
from .walkforward import by_season_folds

PACKAGE_VERSION = C["package_version"]


# ---------------------------------------------------------------------------
# utilitaires
# ---------------------------------------------------------------------------
def outcome_1n2(m):
    return "1" if m["hg"] > m["ag"] else ("N" if m["hg"] == m["ag"] else "2")


def _binary_bundle(probs, outs, label):
    n = len(probs)
    if n == 0:
        return {"label": label, "n": 0, "sample_verdict": "INSUFFICIENT_SAMPLE"}
    brier = sum((p - o) ** 2 + ((1 - p) - (1 - o)) ** 2 for p, o in zip(probs, outs)) / n
    import math
    ll = sum(-math.log(min(max(p if o else 1 - p, 1e-12), 1.0)) for p, o in zip(probs, outs)) / n
    return {"label": label, "n": n, "brier": round(brier, 5), "logloss": round(ll, 5),
            "accuracy": round(sum(1 for p, o in zip(probs, outs) if (p >= 0.5) == bool(o)) / n, 4),
            "ece": round(ece_binary(probs, outs), 5),
            "reliability": reliability_table(probs, outs),
            "sample_verdict": "OK" if n >= C["strong_claim_n"] else (
                "WEAK_SAMPLE" if n >= C["minimum_sample_size"] else "INSUFFICIENT_SAMPLE")}


def _dist_of(pred):
    mk = pred["markets"]
    return {"1": mk["1"], "N": mk["N"], "2": mk["2"]}


# ---------------------------------------------------------------------------
# passe chronologique unique
# ---------------------------------------------------------------------------
def run_pass(matches, folds_scope, elo_k, dc_xi, weight_c=None, fit_dc=True,
             xg_map=None, keep_shadow=False, code_hash=None, dataset_hash=None):
    """Exécute le backtest. Retourne records par pli.
    folds_scope : liste de Fold (walkforward) dont les matchs de test sont
    évalués ; l'état (Elo/forces/historiques) est mis à jour sur TOUS les
    matchs, prédictions enregistrées sur les seuls matchs des plis visés.
    """
    test_ids = {}
    for f in folds_scope:
        for m in f.test:
            test_ids[m["match_id"]] = f.fold_id
    dc_fits = {}          # (league, fold_id) -> params
    dc_fit_done = set()
    fold_by_season = {}
    for f in folds_scope:
        for m in f.test:
            fold_by_season[(m["league"], m["season"])] = f.fold_id

    elo = TemporalElo(k=elo_k)
    strengths = defaultdict(OnlineStrengths)
    histories = {}
    replay = OnlineReplay2A()
    model_b = Model2C_B()
    model_d = Model2C_D(weight_c if weight_c is not None else 0.5) if weight_c is not None else None

    records = []          # une entrée par match de test
    for m in sorted(matches, key=lambda x: x["kickoff_utc"]):
        ko = parse_ts(m["kickoff_utc"])
        home, away, lg = m["home"], m["away"], m["league"]
        st = strengths[lg]
        xg_pair = None
        if xg_map:
            xg_pair = xg_map.get(f"{home}|{away}|{ko.date().isoformat()}")

        fold_id = test_ids.get(m["match_id"])
        if fold_id:
            # --- fit DC au début du pli (train = passé strict du championnat) ---
            if fit_dc and (lg, fold_id) not in dc_fit_done:
                train_lg = [x for x in matches
                            if x["league"] == lg and parse_ts(x["kickoff_utc"]) < ko]
                params = dcm.fit(train_lg, xi=dc_xi, ref_time=ko, xg_map=None)
                dc_fits[(lg, fold_id)] = params
                dc_fit_done.add((lg, fold_id))

            elo_pre = elo.pre_match(home, away)
            sfeats = st.features_pre(home, away, m["kickoff_utc"],
                                     elo_home=elo_pre["elo_home"], elo_away=elo_pre["elo_away"],
                                     xg_available=True)
            feats = build_match_features(
                m, m["kickoff_utc"], elo_pre, sfeats,
                histories.setdefault(home, TeamHistory(home)),
                histories.setdefault(away, TeamHistory(away)),
                xg_for_home=sfeats.get("xg_att_home"), xg_against_home=sfeats.get("xg_def_home"),
                xg_for_away=sfeats.get("xg_att_away"), xg_against_away=sfeats.get("xg_def_away"))

            lvl = data_level(sfeats.get("home_matches") or 0, sfeats.get("away_matches") or 0)
            rec = {"match_id": m["match_id"], "fold_id": fold_id, "league": lg,
                   "season": m["season"], "kickoff_utc": m["kickoff_utc"],
                   "home": home, "away": away, "hg": m["hg"], "ag": m["ag"],
                   "outcome": outcome_1n2(m), "data_level": lvl,
                   "feature_hash": feats["feature_hash"]}
            if not prediction_allowed(lvl):
                rec["prediction_status"] = "NO_PREDICTION"
                rec["reason"] = "INSUFFICIENT_DATA"
            else:
                rec["prediction_status"] = "OK"
                pred_a = Model2C_A().predict(sfeats)
                pred_b = model_b.predict(sfeats)
                rec["A"] = {"dist": _dist_of(pred_a), "markets": pred_a["markets"]}
                rec["B"] = {"dist": _dist_of(pred_b), "markets": pred_b["markets"],
                            "lambda_h": pred_b["lambda_h"], "lambda_a": pred_b["lambda_a"]}
                # B-xG : blend RÉEL si les 4 ratios xG existent
                if all(sfeats.get(k) is not None for k in
                       ("xg_att_home", "xg_def_home", "xg_att_away", "xg_def_away")):
                    def blend(att, xg_att, xg_n):
                        w = min(0.6, (xg_n or 0) / ((xg_n or 0) + 5.0))
                        return (1 - w) * att + w * xg_att
                    b2 = dict(sfeats)
                    b2["att_home"] = blend(sfeats["att_home"], sfeats["xg_att_home"], sfeats.get("xg_matches_home"))
                    b2["def_home"] = blend(sfeats["def_home"], sfeats["xg_def_home"], sfeats.get("xg_matches_home"))
                    b2["att_away"] = blend(sfeats["att_away"], sfeats["xg_att_away"], sfeats.get("xg_matches_away"))
                    b2["def_away"] = blend(sfeats["def_away"], sfeats["xg_def_away"], sfeats.get("xg_matches_away"))
                    pred_bxg = model_b.predict(b2)
                    rec["B_xg"] = {"dist": _dist_of(pred_bxg),
                                   "markets": pred_bxg["markets"]}
                if fit_dc:
                    pc = Model2C_C(dc_fits.get((lg, fold_id))).predict(home, away)
                    if pc:
                        rec["C"] = {"dist": _dist_of(pc), "markets": pc["markets"],
                                    "xg_used": pc.get("xg_used", False)}
                        if model_d:
                            pd_ = model_d.predict(pred_b, pc, home, away)
                            if pd_:
                                rec["D"] = {"dist": _dist_of(pd_), "markets": pd_["markets"],
                                            "weight_c": pd_["weight_c"]}
                r2a = replay.predict_and_update(m) if fold_id else None
                if r2a:
                    rec["2A"] = {"dist": r2a["dist"], "markets": r2a["markets"],
                                 "reliab": r2a["reliab"]}
                if keep_shadow:
                    dq = DataQuality(lvl, sfeats.get("home_matches") or 0,
                                     sfeats.get("away_matches") or 0,
                                     bool((rec.get("C") or {}).get("xg_used")),
                                     sfeats and ["openligadb"] or [])
                    for model_id, key in (("MODEL_2C_A", "A"), ("MODEL_2C_B", "B"),
                                          ("MODEL_2C_C", "C"), ("MODEL_2C_D", "D")):
                        if key in rec:
                            write_shadow(m["match_id"], m["kickoff_utc"], model_id,
                                         f"{model_id}/1.0+{PACKAGE_VERSION}",
                                         {k: round(v, 6) for k, v in rec[key]["dist"].items()},
                                         None, feats["feature_hash"], dataset_hash or "",
                                         dq.as_dict(), (sfeats.get("home_matches") or 0)
                                         + (sfeats.get("away_matches") or 0),
                                         code_hash or "", CALIBRATION_VERSION)
                    db.execute("""INSERT INTO features_2c
                        (match_id, team_id, feature_name, feature_value, feature_json,
                         as_of, source, snapshot_id, feature_version, feature_hash, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (m["match_id"], None, "__vector__", None,
                         json.dumps({k: v for k, v in feats.items() if not k.startswith("_")},
                                    sort_keys=True, ensure_ascii=True),
                         m["kickoff_utc"], ",".join(feats.get("_sources") or []),
                         None, FEATURE_VERSION, feats["feature_hash"],
                         datetime.now(timezone.utc).isoformat()))
            records.append(rec)

        # --- mise à jour des états STRICTEMENT APRÈS prédiction (P0) ---
        if not fold_id:
            replay.predict_and_update(m)
        elo.apply_result(home, away, m["hg"], m["ag"])
        st.update(home, away, m["hg"], m["ag"], m["kickoff_utc"], m["kickoff_utc"],
                  xg_home=(xg_pair[0] if xg_pair else None),
                  xg_away=(xg_pair[1] if xg_pair else None))
        histories.setdefault(home, TeamHistory(home)).add(ko, m["hg"], m["ag"], "H", away)
        histories.setdefault(away, TeamHistory(away)).add(ko, m["ag"], m["hg"], "A", home)
    return records


# ---------------------------------------------------------------------------
# métriques d'un lot d'enregistrements (raw + calibration chaînée)
# ---------------------------------------------------------------------------
def _collect(records, model_key, CALIB={}):
    pts = [(r[model_key]["dist"], r["outcome"], r["match_id"])
           for r in records if r.get("prediction_status") == "OK" and model_key in r]
    return pts


def evaluate_models(records, model_keys, prior_records, calibrate=True):
    """Bundles par modèle. Calibration : Platt+isotone entraînées sur
    prior_records (plis strictement antérieurs — disjointure garantie par
    l'appelant), jamais sur le lot courant."""
    out = {}
    for key in model_keys:
        pts = _collect(records, key)
        pri = _collect(prior_records, key)
        dists = [p[0] for p in pts]
        outs = [p[1] for p in pts]
        entry = {"raw": metric_bundle(dists, outs, f"{key}-raw")}
        if calibrate and len(pri) >= C["isotonic_min_pairs"]:
            for method in ("platt", "isotonic"):
                cal = MulticlassCalibrator(method).fit([p[0] for p in pri], [p[1] for p in pri])
                cd = [cal.predict(d) for d in dists]
                # garde anti-extrêmes (§21) avec support = calibrants au-delà de 0.90
                sup90 = sum(1 for p in pri if max(p[0].values()) > C["extreme_hi"])
                cd = [{k: publish_guard(v, sup90) for k, v in d.items()} for d in cd]
                tot = [{k: v / sum(d.values()) for k, v in d.items()} for d in cd]
                entry[f"calibrated_{method}"] = metric_bundle(tot, outs, f"{key}-{method}")
                entry[f"calibrated_{method}"]["support_extreme_hi"] = sup90
        else:
            entry["calibrated"] = "NOT_TRAINED (pli antérieur insuffisant — honnête)"
        # marchés binaires (O2.5 / BTTS) sur BRUT
        if dists:
            pro, pbt, oov, obt = [], [], [], []
            for r in records:
                if r.get("prediction_status") == "OK" and key in r:
                    mk = r[key]["markets"]
                    pro.append(mk["O2.5"]); oov.append(1 if r["hg"] + r["ag"] >= 3 else 0)
                    pbt.append(mk["BTTS_YES"]); obt.append(1 if (r["hg"] > 0 and r["ag"] > 0) else 0)
            entry["over25"] = _binary_bundle(pro, oov, f"{key}-O2.5")
            entry["btts"] = _binary_bundle(pbt, obt, f"{key}-BTTS")
        out[key] = entry
    return out


def compare_with_2a(records, key_2c):
    """2A rejeu vs 2C sur exactement les mêmes matchs (intersection réelle)."""
    pts = [(r["2A"]["dist"], r[key_2c]["dist"], r["outcome"])
           for r in records if r.get("prediction_status") == "OK"
           and key_2c in r and "2A" in r]
    d2a = [p[0] for p in pts]
    d2c = [p[1] for p in pts]
    outs = [p[2] for p in pts]
    return compare_models(d2a, d2c, outs, "2A_replay", key_2c)


# ---------------------------------------------------------------------------
# backtest complet (sélection hp sur validation, évaluation finale gelée)
# ---------------------------------------------------------------------------
def full_backtest(matches, xg_map=None, code_hash=None, dataset_hash=None,
                  persist=False, val_max_season=2021, progress=None):
    """Exécute : sélection hp (validation) → run final (tous plis) →
    métriques validation/finale séparées → comparaisons 2A → persistance."""
    dataset_hash = dataset_hash or compute_dataset_hash(matches)
    code_hash = code_hash or compute_code_hash()
    folds = by_season_folds(matches)
    val_folds = [f for f in folds if f.test and f.test[0]["season"] <= val_max_season]
    final_folds = [f for f in folds if f.test and f.test[0]["season"] > val_max_season]
    log = (progress or (lambda msg: None))

    # ---- 1) sélection elo_k sur VALIDATION (modèle B, logloss brute) ----
    k_scores = {}
    for k in C["elo_k_candidates"]:
        recs = run_pass(matches, val_folds, elo_k=k, dc_xi=C["dc_xi_default"],
                        fit_dc=False, xg_map=xg_map)
        pts = _collect(recs, "B")
        if not pts:
            k_scores[k] = float("inf")
            continue
        import math
        ll = sum(-math.log(min(max(d[o], 1e-12), 1.0)) for d, o, _ in pts) / len(pts)
        k_scores[k] = ll
        log(f"[hp] elo_k={k} -> logloss B validation = {ll:.4f} (n={len(pts)})")
    elo_k = min(k_scores.items(), key=lambda kv: kv[1])[0] if any(v < float("inf") for v in k_scores.values()) else C["elo_k_default"]

    # ---- 2) sélection dc_xi sur VALIDATION (modèle C) ----
    xi_scores = {}
    for xi in C["dc_xi_candidates"]:
        recs = run_pass(matches, val_folds, elo_k=elo_k, dc_xi=xi, fit_dc=True, xg_map=xg_map)
        pts = _collect(recs, "C")
        if not pts:
            xi_scores[xi] = float("inf")
            continue
        import math
        ll = sum(-math.log(min(max(d[o], 1e-12), 1.0)) for d, o, _ in pts) / len(pts)
        xi_scores[xi] = ll
        log(f"[hp] dc_xi={xi} -> logloss C validation = {ll:.4f} (n={len(pts)})")
    dc_xi = min(xi_scores.items(), key=lambda kv: kv[1])[0] if any(v < float("inf") for v in xi_scores.values()) else C["dc_xi_default"]

    # ---- 3) poids ensemble sur VALIDATION (jamais sur le test) ----
    recs_val = run_pass(matches, val_folds, elo_k=elo_k, dc_xi=dc_xi,
                        weight_c=0.5, fit_dc=True, xg_map=xg_map)
    import math
    def _ll(recs, key):
        pts = _collect(recs, key)
        return (sum(-math.log(min(max(d[o], 1e-12), 1.0)) for d, o, _ in pts) / len(pts)) if pts else float("inf")
    w_scores = {}
    b_c_dists = {r["match_id"]: (r["B"]["dist"], r["C"]["dist"], r["outcome"])
                 for r in recs_val if r.get("prediction_status") == "OK" and "B" in r and "C" in r}
    for w in (0.25, 0.5, 0.75):
        dmix = [{
            "1": w * c["1"] + (1 - w) * b["1"], "N": w * c["N"] + (1 - w) * b["N"],
            "2": w * c["2"] + (1 - w) * b["2"]} for b, c, _ in b_c_dists.values()]
        outs = [o for _, _, o in b_c_dists.values()]
        w_scores[w] = (sum(-math.log(min(max(d[o], 1e-12), 1.0)) for d, o in zip(dmix, outs)) / len(dmix)) if dmix else float("inf")
    weight_c = min(w_scores.items(), key=lambda kv: kv[1])[0] if b_c_dists else None
    ll_b, ll_c = _ll(recs_val, "B"), _ll(recs_val, "C")
    d_justified = bool(weight_c is not None and b_c_dists
                       and w_scores[weight_c] < min(ll_b, ll_c))
    if not d_justified:
        weight_c = None   # NOT_JUSTIFIED — modèle D écarté honnêtement
    log(f"[hp] weight_c={weight_c} d_justified={d_justified}")

    # ---- 4) run FINAL (hyperparamètres gelés) sur TOUS les plis ----
    recs_final = run_pass(matches, folds, elo_k=elo_k, dc_xi=dc_xi,
                          weight_c=weight_c, fit_dc=True, xg_map=xg_map,
                          keep_shadow=persist, code_hash=code_hash,
                          dataset_hash=dataset_hash)
    keys = ["A", "B", "B_xg", "C"] + (["D"] if weight_c is not None else []) + ["2A"]
    val_ids = {m["match_id"] for f in val_folds for m in f.test}
    final_ids = {m["match_id"] for f in final_folds for m in f.test}
    rec_val_final = [r for r in recs_final if r["match_id"] in val_ids]
    rec_test_final = [r for r in recs_final if r["match_id"] in final_ids]

    metrics_val = evaluate_models(rec_val_final, keys, prior_records=[], calibrate=True)
    metrics_fin = evaluate_models(rec_test_final, keys, prior_records=rec_val_final,
                                  calibrate=True)
    # NB honnêteté : la calibration du lot final n'utilise QUE les plis de
    # validation (antérieurs) — jamais le lot final lui-même.

    comparisons = {}
    for key in (["B", "C"] + (["D"] if weight_c is not None else [])):
        comparisons[key] = compare_with_2a(rec_test_final, key)
    comparisons_validation = {}
    for key in ("B", "C"):
        comparisons_validation[key] = compare_with_2a(rec_val_final, key)

    # qualité des données
    levels = defaultdict(int)
    for r in recs_final:
        levels[r["data_level"]] += 1
    xg_cov = {"matches_with_real_xg": sum(1 for r in recs_final if "B_xg" in r),
              "total_test_matches": len(recs_final),
              "note": "couverture StatsBomb BL 23/24 = 34 matchs, TOUS Bayer Leverkusen "
                      "(saison invaincue) → xG deux-équipes jamais justifié (NOT_JUSTIFIED) ; "
                      "ratios unilatéraux RÉELS tracés dans features_2c, mécanisme prouvé par tests"}

    # §27 — par compétition et par saison (sur le lot final, distributions BRUTES
    # 2A-C : échantillon propre par segment, verdict d'échantillon joint)
    per_comp = {}
    seg_records = rec_val_final + rec_test_final
    for league in sorted({r["league"] for r in seg_records}):
        per_comp[league] = {}
        for season in sorted({r["season"] for r in seg_records if r["league"] == league}):
            seg = [r for r in seg_records if r["league"] == league and r["season"] == season]
            segm = {}
            for key in ("2A", "B", "C"):
                pts = [(r[key]["dist"], r["outcome"]) for r in seg
                       if r.get("prediction_status") == "OK" and key in r]
                segm[key] = metric_bundle([p[0] for p in pts], [p[1] for p in pts], key)
            per_comp[league][f"season_{season}"] = segm

    from .features import FEATURE_REGISTRY
    unknown_features = [name for name, m in FEATURE_REGISTRY.items()
                        if m["kind"] == "UNKNOWN"]

    report = {
        "meta": {"generated_at": datetime.now(timezone.utc).isoformat(),
                 "package_version": PACKAGE_VERSION, "status": C["status"],
                 "dataset_hash": dataset_hash, "code_hash": code_hash,
                 "feature_version": FEATURE_VERSION,
                 "calibration_version": CALIBRATION_VERSION},
        "dataset": {"leagues": sorted({m["league"] for m in matches}),
                    "seasons": sorted({m["season"] for m in matches}),
                    "matches": len(matches),
                    "period": {"first": min(m["kickoff_utc"] for m in matches),
                               "last": max(m["kickoff_utc"] for m in matches)}},
        "hyperparams": {"elo_k": elo_k, "elo_k_scores_validation": k_scores,
                        "dc_xi": dc_xi, "dc_xi_scores_validation": xi_scores,
                        "weight_c": weight_c, "ensemble_weights_scores_validation": w_scores,
                        "ensemble_D_justified": d_justified,
                        "selection_scope": f"validation folds seasons<= {val_max_season}"},
        "folds": {"validation": [f.fold_id for f in val_folds],
                  "final": [f.fold_id for f in final_folds]},
        "n_records": {"validation": len(rec_val_final), "final": len(rec_test_final)},
        "metrics_validation": metrics_val,
        "metrics_final": metrics_fin,
        "comparison_2a_final": comparisons,
        "comparison_2a_validation": comparisons_validation,
        "per_competition": per_comp,
        "unknown_features": unknown_features,
        "data_quality": {"levels": dict(levels), "xg_coverage": xg_cov},
        "notes": [
            "Brier/LogLoss/ECE/accuracy calculés par match (moyenne non pondérée) ; N affiché partout.",
            "Calibration entraînée UNIQUEMENT sur plis antérieurs (jamais sur le lot évalué).",
            "xG réel uniquement où StatsBomb couvre ; le reste = UNKNOWN, jamais dérivé.",
            "Rejeu 2A = engine.predict_match sur stats reconstruites sémantiquement identiques (import lecture seule).",
            "ROI non calculé : critère secondaire par décision de cadrage (§18).",
        ],
    }

    # ---- 5) persistance registry (v3) ----
    if persist:
        tw = {"leagues": report["dataset"]["leagues"],
              "seasons": report["dataset"]["seasons"],
              "validation_folds": report["folds"]["validation"],
              "final_folds": report["folds"]["final"]}
        for model_id, key in (("MODEL_2C_A", "A"), ("MODEL_2C_B", "B"),
                              ("MODEL_2C_C", "C"), ("MODEL_2C_D", "D")):
            if key in metrics_fin:
                register_model(model_id, f"1.0+{PACKAGE_VERSION}",
                               {"validation": metrics_val.get(key),
                                "final": metrics_fin.get(key),
                                "comparison_2a": comparisons.get(key)},
                               tw, dataset_hash, code_hash,
                               features={"feature_version": FEATURE_VERSION},
                               status="EXPERIMENTAL")
    return report
