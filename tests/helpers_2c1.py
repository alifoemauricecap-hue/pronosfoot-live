# -*- coding: utf-8 -*-
"""Fixtures partagées des tests 2C.1 (utilitaires — pas un fichier de tests).

Le seed ici est SYNTHÉTIQUE et volontairement minuscule (4 équipes, 36 matchs,
été 2026) : les tests unitaires doivent être rapides et déterministes. Un test
dédié utilise LES VRAIS ASSETS (seed OL 6120 matchs réels).
"""
import json
from datetime import datetime, timedelta, timezone

import db as db_mod

NOW = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)   # « maintenant » figé

TEAMS = ["bayern munchen", "werder bremen", "borussia dortmund", "rb leipzig"]

# scores cycliques déterministes (aucune donnée réelle — marqué SYNTH)
_SCORES = [(2, 1), (0, 0), (1, 3), (2, 2), (3, 0), (1, 1)]


def synth_seed():
    """36 matchs bl1 SYNTHÉTIQUES (round-robin ×3) juin–août 2026."""
    out, k = [], 0
    base = datetime(2026, 6, 6, 15, 30, tzinfo=timezone.utc)
    for rep in range(3):
        for i, h in enumerate(TEAMS):
            for j, a in enumerate(TEAMS):
                if h == a:
                    continue
                hg, ag = _SCORES[k % len(_SCORES)]
                k += 1
                out.append({"match_id": f"synth:{rep}:{i}:{j}", "league": "bl1",
                            "season": 2026,
                            "kickoff_utc": (base + timedelta(days=3 * k)).isoformat(),
                            "home": h, "away": a, "hg": hg, "ag": ag,
                            "effective_at": None, "source": "SYNTH_TEST"})
    out.sort(key=lambda m: m["kickoff_utc"])
    return out


def synth_calibration():
    """Calibration Platt figée SYNTHÉTIQUE (a,b déclarés, jamais fittés ici)."""
    from model2c.shadow_assets import FrozenMulticlassPlatt
    payload = {"n": 120, "classes": {c: {"a": 1.05, "b": -0.03, "n": 120,
                                         "trained": True}
                                     for c in ("1", "N", "2")}}
    return {"calibration_version": "2c-calib-synth-test",
            "models": {m: FrozenMulticlassPlatt(payload)
                       for m in ("MODEL_2C_B", "MODEL_2C_C", "MODEL_2C_D")}}


class StubDC:
    """Params DC factices déterministes (matrix Poisson — pas un fit)."""
    xg_used = False
    n_train = 999

    def predict(self, home, away, max_goals=None):
        from model2c.poisson2c import score_matrix
        from model2c.config2c import CONFIG_2C
        return score_matrix(1.30, 1.10, CONFIG_2C["max_goals_grid"])


_MATCH_COLS = ("id, source, source_match_id, fallback_key, competition, season,"
               " home_team, away_team, home_team_ext_id, away_team_ext_id,"
               " kickoff_time_utc, status, home_score, away_score, home_ht_score,"
               " away_ht_score, venue, evaluation_status, last_payload_json,"
               " last_payload_hash, created_at, updated_at, last_seen_at")


def insert_match(db, mid, home, away, kickoff_iso, competition="ger.1",
                 status="UPCOMING", hg=None, ag=None):
    db.execute(
        f"INSERT INTO matches ({_MATCH_COLS}) VALUES "
        f"({','.join(['?'] * 23)})",
        (mid, "espn", mid.split(":")[-1], None, competition, "2026",
         home, away, "1", "2", kickoff_iso, status, hg, ag, None, None, None,
         "PENDING", None, None, NOW.isoformat(), NOW.isoformat(),
         NOW.isoformat()))
    return mid


def insert_result(db, mid, hg, ag, captured_at=None):
    cap = (captured_at or NOW).isoformat()
    db.execute(
        """INSERT INTO results (match_id, home_score, away_score, home_ht_score,
               away_ht_score, final_status, completed_at, source, captured_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (mid, hg, ag, None, None, "FT", cap, "espn", cap))


def insert_2a_1n2(db, mid, version_seq=1, probs=(0.5, 0.3, 0.2),
                  kickoff_iso=None):
    """Une version 2A 1N2 (3 lignes 1/N/2) + son snapshot (FK)."""
    sid = f"snap:{mid}:{version_seq}"
    db.execute(
        """INSERT INTO data_snapshots (id, match_id, captured_at, as_of_utc,
               source, source_version, payload_json, payload_hash, data_quality,
               available_fields) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sid, mid, NOW.isoformat(), NOW.isoformat(), "espn", "1", "{}", "h",
         None, None))
    dist = {"1": probs[0], "N": probs[1], "2": probs[2]}
    db.execute(
        """INSERT INTO predictions
           (id, match_id, version_seq, prediction_created_at,
            prediction_effective_at, kickoff_time_utc, market, selection,
            raw_probability, published_probability, fair_odds,
            bookmaker_odds, distribution_json, model_name, model_version,
            snapshot_id, snapshot_hash, prediction_hash, prediction_status,
            frozen_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"pred:{mid}:{version_seq}:1N2", mid, version_seq,
         NOW.isoformat(), NOW.isoformat(), kickoff_iso or NOW.isoformat(),
         "1N2", "1", probs[0], probs[0], None, None,
         json.dumps(dist), "poisson", "1.0.0", sid, "sh",
         "ph", "PREDICTION_FROZEN", NOW.isoformat(), NOW.isoformat()))


def fresh_db(tmp_path):
    """DB neuve migrée (v1→v4) + restauration du contexte après test."""
    prev = db_mod.db_path()
    db_mod.init(str(tmp_path / "t2c1.db"), reset=True)
    return prev


def restore_db(prev):
    db_mod.init(prev, reset=True)


def runner(db, seed=None, stub_dc=True, calib="synth", **kw):
    """ShadowRunner de test (seed synth + calibration synth + DC stub)."""
    from model2c.shadow_prod import ShadowRunner
    r = ShadowRunner(db_module=db, store=kw.pop("store", None),
                     max_matches=kw.pop("max_matches", 5),
                     seed=seed if seed is not None else synth_seed(),
                     calibration=(synth_calibration() if calib == "synth"
                                  else calib), **kw)
    if stub_dc:
        r._dc_params = lambda league, now_dt, hist: StubDC()
    return r


def count(db, table, where="1=1", params=()):
    return db.query(f"SELECT COUNT(*) AS c FROM {table} WHERE {where}",
                    params, one=True)["c"]


def rows(db, table, where="1=1", params=()):
    return db.rows_to_dicts(db.query(f"SELECT * FROM {table} WHERE {where}",
                                     params))


def table_hash(db, table):
    """Empreinte SHA-256 du contenu d'une table (ordre stable)."""
    import hashlib
    payload = json.dumps(db.rows_to_dicts(db.query(f"SELECT * FROM {table}")),
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()
