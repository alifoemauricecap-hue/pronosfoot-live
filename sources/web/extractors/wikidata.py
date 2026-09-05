# -*- coding: utf-8 -*-
"""
WIKIDATA EXTRACTOR (2B.WEB-3 §10)
===================================
Rôle : IDENTITÉ uniquement — QID, nom officiel, alias, pays, ville, stade,
identifiants externes. Jamais de statistiques sportives ici (§10).

Supporte DEUX formats réels de l'API :
- wbsearchentities → {"search": [{id, label, description, aliases?}, …]}
- wbgetentities   → {"entities": {QID: {labels, aliases, claims}}}
"""
from ..normalized import SOURCE_NATIVE
from .base import ensure_obj, point


def parse_search(obj, ctx, source="wikidata", lang="fr"):
    """wbsearchentities → candidats d'identité (QID + label)."""
    out = []
    data = ensure_obj(obj)
    for r in (data.get("search") or [] if isinstance(data, dict) else []):
        if not isinstance(r, dict) or not r.get("id"):
            continue
        out.append(point({"qid": r["id"], "label": r.get("label"),
                          "description": r.get("description")},
                         "entity_candidate", source, ctx,
                         level=SOURCE_NATIVE))
    return out


def parse_entity(obj, ctx, source="wikidata", lang="fr"):
    """wbgetentities → identité détaillée d'UNE entité (la 1re)."""
    out = []
    data = ensure_obj(obj)
    ents = (data.get("entities") or {}) if isinstance(data, dict) else {}
    for qid, e in ents.items():
        labels = e.get("labels") or {}
        label = (labels.get(lang) or labels.get("en") or {})
        if label.get("value"):
            out.append(point(label["value"], "entity_official_name",
                             source, ctx, level=SOURCE_NATIVE,
                             team_id=qid))
        aliases = ((e.get("aliases") or {}).get(lang)
                   or (e.get("aliases") or {}).get("en") or [])
        names = [a.get("value") for a in aliases if a.get("value")]
        if names:
            out.append(point(names, "entity_aliases", source, ctx,
                             level=SOURCE_NATIVE, team_id=qid))
        props = e.get("claims") or {}
        def _first(pid):
            cl = props.get(pid) or []
            for c in cl:
                dv = ((c.get("mainsnak") or {}).get("datavalue") or {})
                if isinstance(dv.get("value"), dict):
                    v = dv["value"]
                    if "id" in v:
                        return v["id"]
                    if "amount" in v:
                        return v["amount"]
                elif dv.get("value") is not None:
                    return dv["value"]
            return None
        country = _first("P17")
        if country:
            out.append(point(country, "entity_country_qid", source, ctx,
                             level=SOURCE_NATIVE, team_id=qid))
        venue = _first("P115") or _first("P4664")
        if venue:
            out.append(point(venue, "entity_stadium_qid", source, ctx,
                             level=SOURCE_NATIVE, team_id=qid))
        out.append(point(qid, "entity_qid", source, ctx,
                         level=SOURCE_NATIVE, team_id=qid))
        break                                   # une seule entité attendue
    return out
