# -*- coding: utf-8 -*-
"""EXTRACTORS purs (2B.WEB-3) — aucun réseau ici."""
from .base import FetchContext, ensure_obj, point  # noqa: F401
from . import espn, football_data, open_meteo, openligadb, statsbomb, \
    wikidata  # noqa: F401

__version__ = "2B.WEB-3"
