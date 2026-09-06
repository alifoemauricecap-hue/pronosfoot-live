# -*- coding: utf-8 -*-
"""2C.1 §4/§17/§19 — activation explicite (OFF par défaut), isolation totale
des erreurs shadow, hook scheduler sans casse."""
import os

import pytest
import db

from helpers_2c1 import fresh_db, restore_db, count
from model2c import shadow_hook


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    for name in shadow_hook.ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    prev = fresh_db(tmp_path)
    yield monkeypatch
    restore_db(prev)


class _FakeScheduler:
    def __init__(self):
        self.db = db
        self.store = None


class TestGate:
    def test_default_off(self):
        assert shadow_hook.enabled() is False

    def test_each_env_name_activates(self, monkeypatch):
        for name in shadow_hook.ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        for name in shadow_hook.ENV_NAMES:
            monkeypatch.setenv(name, "1")
            assert shadow_hook.enabled() is True
            monkeypatch.delenv(name, raising=False)
            assert shadow_hook.enabled() is False

    def test_zero_and_garbage_do_not_activate(self, monkeypatch):
        for v in ("0", "no", "false", "", "2", "OUI"):
            monkeypatch.setenv("PRONOFOOT_2C_SHADOW", v)
            assert shadow_hook.enabled() is False

    def test_canary_cap_is_five(self):
        assert shadow_hook.CANARY_MAX_MATCHES == 5

    def test_off_returns_none_and_writes_nothing(self):
        sched = _FakeScheduler()
        out = shadow_hook.after_ingestion_cycle(sched, {"cycle_id": "c"}, None)
        assert out is None
        assert count(db, "model2c_shadow_heartbeats") == 0
        assert count(db, "predictions_2c_shadow") == 0

    def test_off_never_constructs_runner(self, monkeypatch):
        import model2c.shadow_prod as sp

        class Bomb:
            def __init__(self, *a, **k):
                raise AssertionError("runner construit alors que gate OFF")
        monkeypatch.setattr(sp, "ShadowRunner", Bomb)
        # pas d'import-level use : la construction se fait dans le hook
        from model2c import shadow_hook as sh
        out = sh.after_ingestion_cycle(_FakeScheduler(), {}, None)
        assert out is None


class TestErrorIsolation:
    def test_runner_exception_isolated_and_traced(self, monkeypatch):
        import model2c.shadow_prod as sp

        class ExplodingRunner:
            def __init__(self, *a, **k):
                pass

            def run_once(self, **k):
                raise RuntimeError("boom-volontaire")
        monkeypatch.setenv("PRONOFOOT_2C_SHADOW", "1")
        monkeypatch.setattr(sp, "ShadowRunner", ExplodingRunner)
        out = shadow_hook.after_ingestion_cycle(_FakeScheduler(), {"cycle_id": "c"}, None)
        assert out is None
        rows = db.rows_to_dicts(db.query(
            "SELECT level, code FROM model2c_shadow_alerts"))
        assert any(r["code"] == "SHADOW_FATAL_ISOLATED" and r["level"] == "CRITICAL"
                   for r in rows)

    def test_double_failure_still_silent(self, monkeypatch):
        """Même si l'alerte elle-même échoue (DB sans v4) : jamais d'exception."""
        monkeypatch.setenv("PRONOFOOT_2C_SHADOW", "1")

        class BrokenDB:
            def execute(self, *a, **k):
                raise RuntimeError("db cassée")
            store = None
        import model2c.shadow_prod as sp

        class ExplodingRunner:
            def __init__(self, *a, **k):
                pass
            def run_once(self, **k):
                raise ValueError("boom")
        monkeypatch.setattr(sp, "ShadowRunner", ExplodingRunner)
        out = shadow_hook.after_ingestion_cycle(BrokenDB(), {}, None)
        assert out is None


class TestSchedulerHook:
    def _sched(self):
        from sources.web.web4 import scheduler as w4s
        from sources.web.web4 import metrics as w4m

        class _Journal:
            def events_since(self, ts):
                return []

        class _Orch:
            def research_match(self, ctx, as_of=None):
                class R:
                    requests_used = 0
                    errors = []
                    identity = None
                    points = []
                return R()

        class _Store:
            def add_many(self, dps):
                return dps

            def query(self, **k):
                return []

        cfg = w4s.WEB4_CONFIG.copy()
        return w4s.IngestionScheduler(lambda: _Orch(), _Store(), _Journal(),
                                      metrics=w4m.IngestionMetrics(db_module=db),
                                      config=cfg, db_module=db)

    def test_run_cycle_hook_off_summary_unchanged(self, monkeypatch):
        sched = self._sched()
        s = sched.run_cycle(trigger="T", tier="calendar", matches=[])
        assert "shadow2c" not in s            # OFF ⇒ summary strictement inchangé
        assert s["status"] == "DONE"

    def test_run_cycle_hook_exception_never_breaks_cycle(self, monkeypatch):
        monkeypatch.setenv("PRONOFOOT_2C_SHADOW", "1")
        monkeypatch.setattr(shadow_hook, "after_ingestion_cycle",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        sched = self._sched()
        s = sched.run_cycle(trigger="T", tier="calendar", matches=[])
        assert s["status"] == "DONE"          # cycle WEB-4 intact (§4)
        assert "shadow2c" not in s

    def test_run_cycle_hook_on_runs_shadow(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PRONOFOOT_2C_SHADOW", "1")
        sched = self._sched()
        from model2c.shadow_prod import ShadowRunner
        # runner réel sur DB vide de matchs éligibles : 0 prédiction mais
        # heartbeat écrit (preuve d'exécution dans le cycle WEB-4)
        s = sched.run_cycle(trigger="T", tier="calendar", matches=[])
        assert count(db, "model2c_shadow_heartbeats") == 1
        assert isinstance(s.get("shadow2c"), dict)
        assert s.get("shadow2c", {}).get("eligible") == 0
