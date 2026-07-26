#!/usr/bin/env python3
"""Semantic property-search SPARQL SERVICE endpoint for QLever.

Exposes a SPARQL-shaped endpoint on :7002 whose vocabulary mirrors WDQS's
`wikibase:mwapi` so a federated query feels native to query.wikidata.org users.
Given a search phrase it returns semantically-ranked Wikidata properties (P-ids),
matched by embedding similarity over label+description+aliases (pure semantic).

It does NOT implement general SPARQL. It recognizes a fixed set of
`bd:serviceParam` inputs and a fixed set of output-binding triples, extracted by
regex (robust against QLever's prefix expansion and injected VALUES).

Inputs (objects of `bd:serviceParam`):
  mwapi:search    "<phrase>"     (required)
  mwapi:language  "en"           (informational; only en is indexed)
  mwapi:type      "property"     (required to be "property")
  wikibase:limit  "10"           (default 10, capped at MAX_LIMIT)

Outputs (subject var is bound for each ranked hit):
  ?p     wikibase:apiOutputItem  mwapi:item             -> entity URI  .../entity/Pxxx
  ?p     wikibase:apiOutput      mwapi:directProperty   -> direct URI  .../prop/direct/Pxxx
  ?label wikibase:apiOutput      mwapi:label            -> English label literal
  ?score wikibase:apiOrdinal     true                   -> 1-based rank (xsd:integer)
"""
import json
import os
import re

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

# --- load index ---------------------------------------------------------------
VECTORS = np.load(os.path.join(INDEX_DIR, "vectors.npy"))
with open(os.path.join(INDEX_DIR, "meta.json")) as f:
    META = json.load(f)
assert VECTORS.shape[0] == len(META), "vectors/meta length mismatch"


def normalize(text):
    """Lowercase + collapse whitespace for exact lexical matching."""
    return re.sub(r"\s+", " ", text.strip().lower())


# Lexical index: normalized label/alias -> row indices. Pure-semantic ranking
# demotes exact-name matches (a property whose *description* quotes the phrase
# can outscore the one whose *label* IS the phrase), so we pin exact label/alias
# matches to the top of the results.
LEX_INDEX = {}
for _i, _m in enumerate(META):
    _surface = [_m["label"]] + [a for a in _m.get("aliases", "").split(" | ") if a]
    for _s in _surface:
        LEX_INDEX.setdefault(normalize(_s), []).append(_i)

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
QUERY_INSTRUCTION = (
    "Instruct: Given a search phrase, retrieve the Wikidata property whose "
    "meaning best matches it.\nQuery: "
)


def embed(phrase):
    resp = requests.post(
        EMBED_URL,
        json={"model": EMBED_MODEL, "input": [QUERY_INSTRUCTION + phrase]},
        timeout=60,
    )
    resp.raise_for_status()
    v = np.asarray(resp.json()["data"][0]["embedding"], dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


def search(phrase, limit):
    """Return top-`limit` meta rows: exact label/alias matches pinned first
    (ordered among themselves by semantic score), then pure semantic for the
    rest."""
    q = embed(phrase)
    scores = VECTORS @ q

    pinned = sorted(set(LEX_INDEX.get(normalize(phrase), [])), key=lambda i: -scores[i])
    result = list(pinned[:limit])
    seen = set(pinned)
    if len(result) < limit:
        for i in np.argsort(-scores):
            i = int(i)
            if i not in seen:
                result.append(i)
                if len(result) >= limit:
                    break
    return [META[i] for i in result]


# --- SPARQL results JSON ------------------------------------------------------
def build_results(parsed, hits):
    vars_ = []
    for key in ("item_var", "direct_var", "label_var", "score_var"):
        if parsed[key]:
            vars_.append(parsed[key])

    bindings = []
    for rank, hit in enumerate(hits, start=1):
        row = {}
        if parsed["item_var"]:
            row[parsed["item_var"]] = {"type": "uri", "value": hit["uri"]}
        if parsed["direct_var"]:
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
    if parsed["type"] and parsed["type"] != "property":
        return Response("only mwapi:type \"property\" is supported", status=400)

    hits = search(parsed["phrase"], parsed["limit"])
    out = build_results(parsed, hits)
    return Response(json.dumps(out), content_type="application/sparql-results+json")


@app.route("/", methods=["GET"])
def health():
    return Response(
        json.dumps({"status": "ok", "properties": len(META), "dim": int(VECTORS.shape[1])}),
        content_type="application/json",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
