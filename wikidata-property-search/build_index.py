#!/usr/bin/env python3
"""Build the lexical search indices: one for properties, one for "item" entities.

Pulls every Wikidata property (P-id) from the local QLever endpoint together with
its English label, description, and aliases, and separately pulls every entity
that appears as the object of a wdt:P31 ("instance of") triple somewhere in the
dataset (~114k "type" entities like Q5 human, Q515 city, Q4830453 business).
Each set is saved as:

  index/meta.json          [{"pid","uri","label","description","aliases"}, ...]
  index_items/meta.json    [{"qid","uri","label","description","aliases"}, ...]

Running this script always builds both indices, in one invocation.
Re-run this whenever the truthy index is rebuilt.
"""
import os
import sys

import requests

from index_store import atomic_save_json, normalize_entity_row

QLEVER_URL = os.environ.get("QLEVER_URL", "http://localhost:7001")

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_DIR = os.path.join(HERE, "index")
ITEM_INDEX_DIR = os.path.join(HERE, "index_items")

PROPERTY_QUERY = """
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX schema: <http://schema.org/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?p ?label (SAMPLE(?d) AS ?desc)
       (GROUP_CONCAT(DISTINCT ?a; separator=" | ") AS ?aliases) WHERE {
  ?p rdfs:label ?label . FILTER(LANG(?label)="en")
  FILTER(STRSTARTS(STR(?p), "http://www.wikidata.org/entity/P"))
  OPTIONAL { ?p schema:description ?d . FILTER(LANG(?d)="en") }
  OPTIONAL { ?p skos:altLabel ?a . FILTER(LANG(?a)="en") }
} GROUP BY ?p ?label
ORDER BY ?p
"""

# Entities used as the object of "instance of" somewhere in the dataset — a
# bounded "type/class" universe (~114k), not all ~100M Wikidata entities.
# The inner SELECT DISTINCT isolates the cheap, well-indexed P31-object scan
# from the label/description/alias joins. Note: no STRSTARTS/Q-prefix guard
# here — adding one forces per-triple filtering across millions of raw P31
# statements before dedup and blows past a 60s timeout; without it, QLever
# resolves the DISTINCT directly off the index in well under a second, and
# every P31 object in Wikidata's data model is a Q-item anyway.
ITEM_QUERY = """
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX schema: <http://schema.org/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
SELECT ?o ?label (SAMPLE(?d) AS ?desc)
       (GROUP_CONCAT(DISTINCT ?a; separator=" | ") AS ?aliases) WHERE {
  { SELECT DISTINCT ?o WHERE { ?s wdt:P31 ?o } }
  ?o rdfs:label ?label . FILTER(LANG(?label)="en")
  OPTIONAL { ?o schema:description ?d . FILTER(LANG(?d)="en") }
  OPTIONAL { ?o skos:altLabel ?a . FILTER(LANG(?a)="en") }
} GROUP BY ?o ?label
ORDER BY ?o
"""


def fetch_entities(query, var, timeout=300):
    """Query QLever with `query`; return list of dicts keyed off binding `var`."""
    resp = requests.post(
        QLEVER_URL,
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    rows = resp.json()["results"]["bindings"]
    entities = []
    for r in rows:
        uri = r[var]["value"]
        entities.append(
            {
                "id": uri.rsplit("/", 1)[-1],
                "uri": uri,
                "label": r.get("label", {}).get("value", ""),
                "description": r.get("desc", {}).get("value", ""),
                "aliases": r.get("aliases", {}).get("value", ""),
            }
        )
    return entities


def build_and_save(label, entities, out_dir, id_key):
    """Normalize `entities` and save meta.json to out_dir."""
    print(f"  got {len(entities)} {label}", flush=True)

    os.makedirs(out_dir, exist_ok=True)
    # atomic_save_json writes to a temp file then atomically renames, so a
    # crash mid-save can't leave a live index (read by the running server,
    # and appended to by tier-3 persistence) truncated/corrupted.
    meta_path = os.path.join(out_dir, "meta.json")
    meta = [normalize_entity_row(e, id_key) for e in entities]
    atomic_save_json(meta_path, meta)

    print(f"Saved {len(meta)} rows to {out_dir}", flush=True)


def main():
    print(f"Fetching properties from {QLEVER_URL} ...", flush=True)
    props = fetch_entities(PROPERTY_QUERY, "p")
    build_and_save("properties", props, INDEX_DIR, "pid")

    print(f"Fetching P31-object items from {QLEVER_URL} ...", flush=True)
    items = fetch_entities(ITEM_QUERY, "o", timeout=900)
    build_and_save("items", items, ITEM_INDEX_DIR, "qid")


if __name__ == "__main__":
    sys.exit(main())
