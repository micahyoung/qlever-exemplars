#!/usr/bin/env python3
"""Semantic property/item-search SPARQL SERVICE endpoint for QLever.

Exposes a SPARQL-shaped endpoint on :7002 whose vocabulary mirrors WDQS's
`wikibase:mwapi` so a federated query feels native to query.wikidata.org users.
Given a search phrase it returns semantically-ranked hits, matched by embedding
similarity over label+description+aliases (pure semantic).

Two independent indices are served, selected by `mwapi:type`:
  "property" (default) - all ~13.5k Wikidata properties
  "item"               - the ~114k entities used as the object of a wdt:P31
                          ("instance of") triple somewhere in the dataset, i.e.
                          "type/class" entities like Q5 (human), Q515 (city).
                          This is NOT general entity search over all ~100M
                          Wikidata entities - see CLAUDE.md for why.

It does NOT implement general SPARQL. It recognizes a fixed set of
`bd:serviceParam` inputs and a fixed set of output-binding triples, extracted by
regex (robust against QLever's prefix expansion and injected VALUES).

Inputs (objects of `bd:serviceParam`):
  mwapi:search    "<phrase>"     (required)
  mwapi:language  "en"           (informational; only en is indexed)
  mwapi:type      "property"|"item"  (default "property" if omitted)
  wikibase:limit  "10"           (default 10, capped at MAX_LIMIT)

Outputs (subject var is bound for each ranked hit):
  ?p     wikibase:apiOutputItem  mwapi:item             -> entity URI  .../entity/Pxxx or Qxxx
  ?p     wikibase:apiOutput      mwapi:directProperty   -> direct URI  .../prop/direct/Pxxx (properties only)
  ?label wikibase:apiOutput      mwapi:label            -> English label literal
  ?score wikibase:apiOrdinal     true                   -> 1-based rank (xsd:integer)
"""
import json
import os
import re
from collections import namedtuple

import numpy as np
import requests
from flask import Flask, request, Response

EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8888/v1/embeddings")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "qwen3-embedding-8b")
PORT = int(os.environ.get("PORT", "7002"))
MAX_LIMIT = 50
DEFAULT_LIMIT = 10

ENTITY_PREFIX = "http://www.wikidata.org/entity/"
DIRECT_PREFIX = "http://www.wikidata.org/prop/direct/"

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_DIR = os.path.join(HERE, "index")
ITEM_INDEX_DIR = os.path.join(HERE, "index_items")


def normalize(text):
    """Lowercase + collapse whitespace for exact lexical matching."""
    return re.sub(r"\s+", " ", text.strip().lower())


IndexBundle = namedtuple("IndexBundle", ["vectors", "meta", "lex_index", "instruction"])


def load_index_bundle(dir_path, instruction, required):
    """Load vectors.npy + meta.json from dir_path into an IndexBundle.

    Returns None (instead of raising) if the directory/files don't exist and
    `required` is False, so the server can still start and serve the other
    index while e.g. the item index hasn't been built yet.
    """
    vec_path = os.path.join(dir_path, "vectors.npy")
    meta_path = os.path.join(dir_path, "meta.json")
    if not (os.path.exists(vec_path) and os.path.exists(meta_path)):
        if required:
            raise FileNotFoundError(f"required index missing at {dir_path}")
        print(f"WARNING: index not found at {dir_path}, skipping", flush=True)
        return None

    vectors = np.load(vec_path)
    with open(meta_path) as f:
        meta = json.load(f)
    assert vectors.shape[0] == len(meta), f"vectors/meta length mismatch in {dir_path}"

    # Lexical index: normalized label/alias -> row indices. Pure-semantic
    # ranking demotes exact-name matches (a hit whose *description* quotes the
    # phrase can outscore the one whose *label* IS the phrase), so we pin
    # exact label/alias matches to the top of the results.
    lex_index = {}
    for i, m in enumerate(meta):
        surface = [m["label"]] + [a for a in m.get("aliases", "").split(" | ") if a]
        for s in surface:
            lex_index.setdefault(normalize(s), []).append(i)

    return IndexBundle(vectors, meta, lex_index, instruction)


# --- load indices --------------------------------------------------------------
PROPERTY_INSTRUCTION = (
    "Instruct: Given a search phrase, retrieve the Wikidata property whose "
    "meaning best matches it.\nQuery: "
)
ITEM_INSTRUCTION = (
    "Instruct: Given a search phrase, retrieve the Wikidata item (entity) whose "
    "meaning best matches it.\nQuery: "
)

PROPERTY_INDEX = load_index_bundle(INDEX_DIR, PROPERTY_INSTRUCTION, required=True)
ITEM_INDEX = load_index_bundle(ITEM_INDEX_DIR, ITEM_INSTRUCTION, required=False)

INDEXES = {"property": PROPERTY_INDEX, "item": ITEM_INDEX}

app = Flask(__name__)

# --- request parsing ----------------------------------------------------------
# Each term may arrive prefixed (mwapi:search) or as a full IRI after QLever
# expands prefixes (<https://www.mediawiki.org/ontology#API/search>).
MWAPI = "https://www.mediawiki.org/ontology#API/"
WIKIBASE = "http://wikiba.se/ontology#"


def _term(prefix_form, full_iri):
    """Regex fragment matching either a prefixed term or its full IRI."""
    return r"(?:%s|<%s>)" % (re.escape(prefix_form), re.escape(full_iri))


def _param(query, prefix_form, local):
    """Extract a quoted serviceParam object value, or None."""
    pat = _term(prefix_form, MWAPI + local) if prefix_form.startswith("mwapi:") \
        else _term(prefix_form, WIKIBASE + local)
    m = re.search(pat + r'\s+"([^"]*)"', query)
    return m.group(1) if m else None


def _output_var(query, pred_prefix, pred_iri, obj_prefix, obj_iri):
    """Find ?var on a line `?var <pred> <obj> .`; return var name (no '?')."""
    pat = (
        r"(\?\w+)\s+" + _term(pred_prefix, pred_iri) + r"\s+" + _term(obj_prefix, obj_iri)
    )
    m = re.search(pat, query)
    return m.group(1)[1:] if m else None


def parse_query(query):
    """Extract search params and output variable names from a SERVICE query."""
    phrase = _param(query, "mwapi:search", "search")
    limit_s = _param(query, "wikibase:limit", "limit")
    ptype = _param(query, "mwapi:type", "type")
    try:
        limit = int(limit_s) if limit_s else DEFAULT_LIMIT
    except ValueError:
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))

    item_var = _output_var(
        query, "wikibase:apiOutputItem", WIKIBASE + "apiOutputItem", "mwapi:item", MWAPI + "item"
    )
    direct_var = _output_var(
        query, "wikibase:apiOutput", WIKIBASE + "apiOutput",
        "mwapi:directProperty", MWAPI + "directProperty",
    )
    label_var = _output_var(
        query, "wikibase:apiOutput", WIKIBASE + "apiOutput", "mwapi:label", MWAPI + "label"
    )
    # apiOrdinal's object is the literal/boolean `true`.
    m = re.search(
        r"(\?\w+)\s+" + _term("wikibase:apiOrdinal", WIKIBASE + "apiOrdinal")
        + r'\s+(?:true|"true")',
        query,
    )
    score_var = m.group(1)[1:] if m else None

    return {
        "phrase": phrase,
        "type": ptype,
        "limit": limit,
        "item_var": item_var,
        "direct_var": direct_var,
        "label_var": label_var,
        "score_var": score_var,
    }


# --- search -------------------------------------------------------------------
# Qwen3-Embedding is instruction-tuned: the QUERY side must carry a task
# instruction, while documents are embedded plain (as build_index.py does).
# Without this prefix the bare-phrase vector is poorly aligned with the document
# vectors and returns near-random matches.
def embed(phrase, instruction):
    resp = requests.post(
        EMBED_URL,
        json={"model": EMBED_MODEL, "input": [instruction + phrase]},
        timeout=60,
    )
    resp.raise_for_status()
    v = np.asarray(resp.json()["data"][0]["embedding"], dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


def search(phrase, limit, bundle):
    """Return top-`limit` meta rows from `bundle`: exact label/alias matches
    pinned first (ordered among themselves by semantic score), then pure
    semantic for the rest."""
    q = embed(phrase, bundle.instruction)
    scores = bundle.vectors @ q

    pinned = sorted(
        set(bundle.lex_index.get(normalize(phrase), [])), key=lambda i: -scores[i]
    )
    result = list(pinned[:limit])
    seen = set(pinned)
    if len(result) < limit:
        for i in np.argsort(-scores):
            i = int(i)
            if i not in seen:
                result.append(i)
                if len(result) >= limit:
                    break
    return [bundle.meta[i] for i in result]


# --- SPARQL results JSON ------------------------------------------------------
def build_results(parsed, hits):
    # mwapi:directProperty (the wdt: predicate form) is a property-only
    # concept - there's no "/prop/direct/Qxxx" form for items. Fail safe by
    # never emitting it for item results, rather than returning a bogus URI.
    emit_direct = parsed["direct_var"] and parsed["type"] != "item"

    vars_ = []
    for key, enabled in (
        ("item_var", parsed["item_var"]),
        ("direct_var", emit_direct),
        ("label_var", parsed["label_var"]),
        ("score_var", parsed["score_var"]),
    ):
        if enabled:
            vars_.append(parsed[key])

    bindings = []
    for rank, hit in enumerate(hits, start=1):
        row = {}
        if parsed["item_var"]:
            row[parsed["item_var"]] = {"type": "uri", "value": hit["uri"]}
        if emit_direct:
            direct = hit["uri"].replace(ENTITY_PREFIX, DIRECT_PREFIX)
            row[parsed["direct_var"]] = {"type": "uri", "value": direct}
        if parsed["label_var"]:
            row[parsed["label_var"]] = {
                "type": "literal", "xml:lang": "en", "value": hit["label"]
            }
        if parsed["score_var"]:
            row[parsed["score_var"]] = {
                "type": "literal",
                "datatype": "http://www.w3.org/2001/XMLSchema#integer",
                "value": str(rank),
            }
        bindings.append(row)

    return {"head": {"vars": vars_}, "results": {"bindings": bindings}}


def get_query():
    """Pull the SPARQL query from any encoding QLever might use."""
    if request.method == "GET":
        return request.args.get("query", "")
    ctype = request.content_type or ""
    if "application/sparql-query" in ctype:
        return request.get_data(as_text=True)
    # form-encoded `query=...`
    return request.form.get("query") or request.args.get("query", "") \
        or request.get_data(as_text=True)


@app.route("/sparql", methods=["GET", "POST"])
def sparql():
    query = get_query()
    parsed = parse_query(query)

    if not parsed["phrase"]:
        return Response(
            json.dumps({"head": {"vars": []}, "results": {"bindings": []}}),
            content_type="application/sparql-results+json",
        )

    ptype = parsed["type"] or "property"  # default when mwapi:type is omitted
    if ptype not in INDEXES:
        return Response(
            f'unsupported mwapi:type "{ptype}" (expected "property" or "item")',
            status=400,
        )
    bundle = INDEXES[ptype]
    if bundle is None:
        return Response(
            f'mwapi:type "{ptype}" index is not loaded on this server', status=503
        )

    hits = search(parsed["phrase"], parsed["limit"], bundle)
    out = build_results(parsed, hits)
    return Response(json.dumps(out), content_type="application/sparql-results+json")


@app.route("/", methods=["GET"])
def health():
    return Response(
        json.dumps({
            "status": "ok",
            "properties": len(PROPERTY_INDEX.meta),
            "dim": int(PROPERTY_INDEX.vectors.shape[1]),
            "items": len(ITEM_INDEX.meta) if ITEM_INDEX else 0,
            "item_index_loaded": ITEM_INDEX is not None,
        }),
        content_type="application/json",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
