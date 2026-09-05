# -*- coding: utf-8 -*-
"""
FOOTBALL-DATA.CO.UK EXTRACTOR (2B.WEB-3 §6/§21)
=================================================
Parse les CSV historiques (E0.csv…) — 132 colonnes réelles vérifiées WEB-0.

Résultats : Div/Date/Time/HomeTeam/AwayTeam/FTHG/FTAG/FTR/HTHG/HTAG/HTR/
Referee + stats HS,AS,HST,AST,HF,AF,HC,AC,HY,AY,HR,AR.

Cotes (1X2) : bookmaker = PRÉFIXE de colonne (B365H → Bet365 …).
Timing — CONVENTION OFFICIELLE football-data.co.uk (notes du site) :
les cotes de CLÔTURE portent le suffixe « C » (B365CH, PSCH…). Les colonnes
sans « C » sont les cotes correspondantes relevées plus tôt — marquées
« opening » UNIQUEMENT si la colonne closing « C » correspondante existe
dans le fichier ; sinon timing="listed" (NON VÉRIFIÉ — jamais d'invention).
Jamais d'écrasement : chaque (bookmaker, market, selection, timing) reste
une entrée séparée (§6).
"""
import csv
import io
import re

from ..normalized import SOURCE_NATIVE, AGGREGATED
from .base import point

_TEAM_M = {"H": "HOME", "D": "DRAW", "A": "AWAY"}
_BOOKS = {"B365": "Bet365", "BF": "Betfair", "BFD": "Betfair Exchange",
          "PS": "Pinnacle", "PSC": "Pinnacle", "WH": "William Hill",
          "VC": "BetVictor", "BW": "Betway", "IW": "Interwetten",
          "LB": "Ladbrokes", "SB": "Stan James", "SJ": "Stan James",
          "GB": "Gamebookers", "SY": "Sportingbet", "BS": "Blue Square",
          "SO": "Sporting Odds", "BB": "Boylesports", "PC": "Paddy Power",
          "B365C": "Bet365", "WHC": "William Hill", "VCC": "BetVictor",
          "PSCH": "Pinnacle"}
_ODD_RE = re.compile(r"^([A-Z]{1,6}C?)(H|D|A)$")
_STAT_MAP = {"HS": "shots", "AS": "shots", "HST": "shots_on_target",
             "AST": "shots_on_target", "HF": "fouls", "AF": "fouls",
             "HC": "corners", "AC": "corners", "HY": "cards_yellow",
             "AY": "cards_yellow", "HR": "cards_red", "AR": "cards_red"}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _iso_date(d):
    """dd/mm/yyyy (fd.co.uk) → ISO."""
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            from datetime import datetime
            return datetime.strptime(d.strip(), fmt).strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            continue
    return None


def parse_csv(text, ctx, source="football_data_co_uk"):
    """Retourne (matches_rows, datapoints). rows = dicts normalisés pour
    l'agrégation de forme (§19)."""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("latin-1", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows, dps = [], []
    # colonnes de cotes présentes → timing déduit selon convention officielle
    cols = set(reader.fieldnames or [])
    return _rows(reader, cols, rows, dps, ctx, source)


def _rows(reader, cols, rows, dps, ctx, source):
    season = ctx.extras.get("season")
    for r in reader:
        iso = _iso_date(r.get("Date", ""))
        home, away = (r.get("HomeTeam") or "").strip() or None, \
                     (r.get("AwayTeam") or "").strip() or None
        if not (home and away):
            continue
        row = {"date": iso, "time": (r.get("Time") or None), "home": home,
               "away": away, "fthg": _i(r.get("FTHG")),
               "ftag": _i(r.get("FTAG")), "ftr": r.get("FTR") or None,
               "hthg": _i(r.get("HTHG")), "htag": _i(r.get("HTAG")),
               "referee": (r.get("Referee") or None), "season": season,
               "competition": (r.get("Div") or "").strip() or None,
               "stats": {}, "odds": []}
        for col, dtype in _STAT_MAP.items():
            v = _i(r.get(col))
            if v is not None:
                side = "home" if col.startswith("H") else "away"
                row["stats"].setdefault(side, {})[dtype] = v
        for col in cols:
            m = _ODD_RE.match(col or "")
            if not m:
                continue
            book, sel = m.group(1), m.group(2)
            val = _f(r.get(col))
            if val is None:
                continue
            closing = book.endswith("C")
            base = book[:-1] if closing else book
            if closing:
                timing = "closing"
            else:
                timing = ("opening" if f"{book}C{sel}" in cols
                          else "listed")   # NON VÉRIFIÉ sinon
            row["odds"].append({
                "bookmaker": _BOOKS.get(book) or _BOOKS.get(base) or base,
                "market": "1X2", "selection": _TEAM_M[sel],
                ("closing" if timing == "closing" else "opening" if
                 timing == "opening" else "listed"): val})
        rows.append(row)
    return rows, dps
