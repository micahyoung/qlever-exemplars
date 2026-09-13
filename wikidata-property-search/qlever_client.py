#!/usr/bin/env python3
"""Tier-3 item/entity verification: live queries against QLever itself,
rather than the (necessarily bounded) local item index.

This is what lets item resolution escape the fixed ~114k-entity ceiling:
instead of trusting the LLM's proposed canonical name, we require it to
exactly match a real rdfs:label already in the dataset.

For relation-pair resolution (a property + item resolved jointly for the
"?s prop item" idiom), triple_exists() adds a further live check: even once
the property and item are each individually verified to exist, the pair as
a whole might not co-occur (e.g. a plausible-but-wrong property paired with
a real item). Verifying the actual triple catches that case.

Disambiguation note: a live label lookup can return dozens of same-labeled
candidates (e.g. "New York City" matched 76 distinct QIDs in testing, mostly
minor streets/buildings, not the real city). rank_by_local_notability
disambiguates by statement count -- but ONLY within that small, already
same-label-filtered candidate set. This is deliberately NOT a general
out-degree ranking: applied globally across all of Wikidata, statement count
is dominated by bulk-imported bio/gene/taxonomy data (verified separately:
top global out-degree entities were things like a mouse gene-trap database
entry with 8437 statements), which has nothing to do with real-world
notability. Scoped to one exact label, it's a legitimate tie-break signal.
"""
import os

import requests

QLEVER_URL = os.environ.get("QLEVER_URL", "http://localhost:7001")
QLEVER_TIER3_TIMEOUT = float(os.environ.get("QLEVER_TIER3_TIMEOUT", "20"))

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# SPARQL 1.1 ECHAR production for a double-quoted string literal.
_ECHAR_MAP = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def escape_sparql_literal(s):
    """Escape a string for safe interpolation inside a double-quoted SPARQL
    string literal ("..."). Handles backslash, double-quote, and the
    control characters that would otherwise break out of the literal."""
    return "".join(_ECHAR_MAP.get(c, c) for c in s)


def _run_query(query, *, url=None, timeout=None):
    resp = requests.post(
        url or QLEVER_URL,
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=timeout if timeout is not None else QLEVER_TIER3_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["results"]["bindings"]


def lookup_label_candidates(name, *, url=None, timeout=None):
    """Live exact rdfs:label@en match against QLever. Returns list[str] of
    entity URIs (possibly empty, possibly more than one)."""
    escaped = escape_sparql_literal(name)
    query = (
        'PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n'
        "SELECT ?item WHERE { ?item rdfs:label \"" + escaped + '"@en }'
    )
    bindings = _run_query(query, url=url, timeout=timeout)
    return [b["item"]["value"] for b in bindings]


def rank_by_local_notability(uris, *, url=None, timeout=None):
    """Given a SMALL pre-filtered candidate set (same-label cluster), return
    the single URI with the highest statement (triple) count. Skips the
    round-trip entirely when there's nothing to disambiguate."""
    if not uris:
        return None
    if len(uris) == 1:
        return uris[0]

    values = " ".join(f"<{u}>" for u in uris)
    query = (
        "SELECT ?s (COUNT(*) AS ?c) WHERE { VALUES ?s { " + values + " } "
        "?s ?p ?o . } GROUP BY ?s ORDER BY DESC(?c) LIMIT 1"
    )
    bindings = _run_query(query, url=url, timeout=timeout)
    if not bindings:
        return uris[0]
    return bindings[0]["s"]["value"]


def resolve_entity_uri(name, *, lookup=lookup_label_candidates, rank=rank_by_local_notability, **kwargs):
    """Compose lookup + disambiguation: returns a single resolved URI, or
    None if `name` matched nothing live in QLever."""
    candidates = lookup(name, **kwargs)
    if not candidates:
        return None
    return rank(candidates, **kwargs)


def triple_exists(prop_uri, item_uri, *, url=None, timeout=None):
    """Live existence check for `?s <prop_uri> <item_uri>` -- used by
    relation-pair tier-3 resolution to verify a resolved property+item pair
    actually co-occurs in the dataset, not just that each half individually
    exists. Uses ASK for the cheapest possible round trip (no result-row
    materialization). ASK's response shape ({"boolean": ...}) differs from
    SELECT's ({"results": {"bindings": ...}}), so this can't go through
    _run_query."""
    query = f"ASK {{ ?s <{prop_uri}> <{item_uri}> }}"
    resp = requests.post(
        url or QLEVER_URL,
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=timeout if timeout is not None else QLEVER_TIER3_TIMEOUT,
    )
    resp.raise_for_status()
    return bool(resp.json().get("boolean"))


def fetch_entity_for_index(uri, *, url=None, timeout=None):
    """Single-QID variant of build_index.py's ITEM_QUERY: fetch label,
    description, and aliases for one known entity URI, shaped identically
    to build_index.fetch_entities()'s per-row dicts so it can be embedded
    and appended via index_store.append_row without any reshaping."""
    query = (
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "PREFIX schema: <http://schema.org/>\n"
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
        "SELECT ?label (SAMPLE(?d) AS ?desc) "
        '(GROUP_CONCAT(DISTINCT ?a; separator=" | ") AS ?aliases) WHERE {\n'
        f"  VALUES ?o {{ <{uri}> }}\n"
        '  ?o rdfs:label ?label . FILTER(LANG(?label)="en")\n'
        '  OPTIONAL { ?o schema:description ?d . FILTER(LANG(?d)="en") }\n'
        '  OPTIONAL { ?o skos:altLabel ?a . FILTER(LANG(?a)="en") }\n'
        "} GROUP BY ?label"
    )
    bindings = _run_query(query, url=url, timeout=timeout)
    if not bindings:
        return None
    b = bindings[0]
    return {
        "id": uri.rsplit("/", 1)[-1],
        "uri": uri,
        "label": b.get("label", {}).get("value", ""),
        "description": b.get("desc", {}).get("value", ""),
        "aliases": b.get("aliases", {}).get("value", ""),
    }
