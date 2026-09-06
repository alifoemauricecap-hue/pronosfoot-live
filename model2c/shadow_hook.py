# -*- coding: utf-8 -*-
"""
ÉTAPE 2C.1 — POINT D'ENTRÉE DU SHADOW DANS LES CYCLES WEB-4 (§4/§12)
=====================================================================
- Activation EXPLICITE uniquement : PRONOFOOT_2C_SHADOW=1 (convention projet)
  — alias acceptés : « 2C_SHADOW=1 » et « SHADOW_2C=1 » (libellés mission).
  Défaut = OFF : coût nul, zéro SQL, zéro effet de bord.
- JAMAIS d'exception vers WEB-4/2A : toute erreur 2C est isolée ici (§17) —
  le Live, le SSE et les prédictions 2A continuent quoi qu'il arrive.
- Canary : ≤ 5 matchs par exécution, aucune requête réseau nouvelle.
"""

import os

ENV_NAMES = ("PRONOFOOT_2C_SHADOW", "2C_SHADOW", "SHADOW_2C")
CANARY_MAX_MATCHES = 5


def enabled():
    """True UNIQUEMENT si une variable d'activation vaut 1/true/yes."""
    for name in ENV_NAMES:
        if (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


def after_ingestion_cycle(scheduler, summary, now):
    """Appelé en FIN de IngestionScheduler.run_cycle (hook additif, 3 lignes
    côté scheduler). Ne lève JAMAIS — dernière ligne de défense §4/§17."""
    try:
        if not enabled():
            return None
    except Exception:
        return None
    try:
        from .shadow_prod import ShadowRunner
        runner = ShadowRunner(db_module=scheduler.db,
                              store=getattr(scheduler, "store", None),
                              max_matches=CANARY_MAX_MATCHES)
        return runner.run_once(now=now, cycle_id=(summary or {}).get("cycle_id"))
    except Exception as e:
        # isolement total : on tente de tracer, puis on avale (§17)
        try:
            import json
            from datetime import datetime, timezone
            scheduler.db.execute(
                """INSERT INTO model2c_shadow_alerts
                   (level, code, detail_json, at_utc) VALUES (%s,%s,%s,%s)""",
                ("CRITICAL", "SHADOW_FATAL_ISOLATED",
                 json.dumps({"err": f"{type(e).__name__}: {e}"}),
                 datetime.now(timezone.utc).isoformat()))
        except Exception:
            pass
        print(f"[2c-shadow] ERREUR ISOLÉE (WEB-4/2A continus) : "
              f"{type(e).__name__}: {e}")
        return None
