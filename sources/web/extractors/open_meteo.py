# -*- coding: utf-8 -*-
"""
OPEN-METEO EXTRACTOR (2B.WEB-3 §7)
====================================
Météo TEMPORELLE : chaque valeur porte effective_at = l'heure de la mesure /
prévision (JAMAIS retrieved_at). Une météo récupérée APRÈS le match est
inutilisable en pré-match — l'anti-leakage (usable_at) l'interdit (§14).

Parse `current` et `hourly` (forecast API v1). target_time optionnel
(heure du match) : sélection de la tranche horaire LA PLUS PROCHE —
documentée dans issues (MATCH_TIME_APPROXIMATION si > 30 min d'écart).
"""
from datetime import datetime, timezone

from ..normalized import SOURCE_NATIVE
from .base import ensure_obj, point

UTC = timezone.utc
_FIELDS = {"temperature_2m": "temperature",
           "apparent_temperature": "apparent_temperature",
           "relative_humidity_2m": "humidity",
           "precipitation": "precipitation",
           "precipitation_probability": "precipitation_probability",
           "wind_speed_10m": "wind_speed",
           "weather_code": "weather_code"}


def _sec(iso):
    """Secondes epoch d'un ISO — accepte «T HH:MM[Z]» et «T HH:MM:SS[.fff]»."""
    s = str(iso).strip().rstrip("Z").rstrip("z")[:19]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return int(datetime.strptime(s, fmt).replace(
                tzinfo=UTC).timestamp())
        except ValueError:
            continue
    raise ValueError(f"ISO illisible : {iso!r}")


def parse_weather(obj, ctx, source="open_meteo", target_time=None):
    out = []
    data = ensure_obj(obj)
    if not isinstance(data, dict):
        return out
    lat, lon = data.get("latitude"), data.get("longitude")
    if lat is not None and lon is not None:
        out.append(point({"lat": lat, "lon": lon}, "geo_point", source, ctx,
                         level=SOURCE_NATIVE,
                         effective_at=ctx.retrieved_at))
    cur = data.get("current")
    if isinstance(cur, dict) and cur.get("time"):
        for f, dtype in _FIELDS.items():
            if f in cur and cur[f] is not None:
                out.append(point(cur[f], dtype, source, ctx,
                                 level=SOURCE_NATIVE,
                                 effective_at=cur["time"]))
        return out
    hourly = data.get("hourly")
    if isinstance(hourly, dict) and hourly.get("time"):
        times = hourly["time"]
        idx = None
        if target_time:
            tsec = _sec(target_time)
            try:
                idx = min(range(len(times)),
                          key=lambda i: abs(_sec(times[i]) - tsec))
                approx = abs(_sec(times[idx]) - tsec)
            except (ValueError, IndexError):
                idx = None
        else:
            idx = 0
        if idx is not None:
            for f, dtype in _FIELDS.items():
                arr = hourly.get(f)
                if isinstance(arr, list) and idx < len(arr) \
                        and arr[idx] is not None:
                    dp = point(arr[idx], dtype, source, ctx,
                               level=SOURCE_NATIVE,
                               effective_at=times[idx])
                    if target_time:
                        gap = abs(_sec(times[idx]) - _sec(target_time))
                        if gap > 1800:
                            dp.issues.append("MATCH_TIME_APPROXIMATION")
                            dp.confidence = "medium"
                    out.append(dp)
    return out
