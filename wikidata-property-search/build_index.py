#!/usr/bin/env python3
"""Build the semantic property-search index.

Pulls every Wikidata property (P-id) from the local QLever endpoint together with
its English label, description, and aliases, embeds a composed text per property
via the local OpenAI-compatible embedding server, and saves:

  index/vectors.npy   float32 [N, 4096], L2-normalized (one row per property)
  index/meta.json     [{"pid","uri","label","description"}, ...] in the same order

Re-run this whenever the truthy index is rebuilt.
"""
import json
import os
import sys

import numpy as np
import requests

QLEVER_URL = os.environ.get("QLEVER_URL", "http://localhost:7001")
EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8888/v1/embeddings")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "qwen3-embedding-8b")
BATCH = int(os.environ.get("EMBED_BATCH", "64"))

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_DIR = os.path.join(HERE, "index")

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


def fetch_properties():
    """Query QLever for all properties; return list of dicts."""
    resp = requests.post(
        QLEVER_URL,
        data={"query": PROPERTY_QUERY},
        headers={"Accept": "application/sparql-results+json"},
        timeout=300,
    )
    resp.raise_for_status()
    rows = resp.json()["results"]["bindings"]
    props = []
    for r in rows:
        uri = r["p"]["value"]
        props.append(
            {
                "pid": uri.rsplit("/", 1)[-1],
                "uri": uri,
                "label": r.get("label", {}).get("value", ""),
                "description": r.get("desc", {}).get("value", ""),
                "aliases": r.get("aliases", {}).get("value", ""),
            }
        )
    return props


def embedding_text(prop):
    """Compose the text that represents a property for semantic matching.

    This is the main quality knob: description carries the semantic signal,
    aliases broaden recall. Tune here if recall is weak.
    """
    parts = [prop["label"]]
    if prop["description"]:
        parts.append(prop["description"])
    if prop["aliases"]:
        parts.append("Also known as: " + prop["aliases"])
    return ". ".join(parts)


def embed_batch(texts):
    """Embed a list of strings via the local embedding server."""
    resp = requests.post(
        EMBED_URL,
        json={"model": EMBED_MODEL, "input": texts},
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    # Preserve request order via the "index" field.
    data.sort(key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def main():
    print(f"Fetching properties from {QLEVER_URL} ...", flush=True)
    props = fetch_properties()
    print(f"  got {len(props)} properties", flush=True)

    texts = [embedding_text(p) for p in props]
    vectors = []
    for i in range(0, len(texts), BATCH):
        chunk = texts[i : i + BATCH]
        vectors.extend(embed_batch(chunk))
        print(f"  embedded {min(i + BATCH, len(texts))}/{len(texts)}", flush=True)

    mat = np.asarray(vectors, dtype=np.float32)
    # Server returns L2-normalized vectors; normalize again defensively so
    # cosine similarity == dot product.
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms

    os.makedirs(INDEX_DIR, exist_ok=True)
    np.save(os.path.join(INDEX_DIR, "vectors.npy"), mat)
    meta = [
        {
            "pid": p["pid"],
            "uri": p["uri"],
            "label": p["label"],
            "description": p["description"],
            "aliases": p["aliases"],  # " | "-separated; used for the lexical pin
        }
        for p in props
    ]
    with open(os.path.join(INDEX_DIR, "meta.json"), "w") as f:
        json.dump(meta, f)

    print(f"Saved {mat.shape[0]} vectors of dim {mat.shape[1]} to {INDEX_DIR}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
