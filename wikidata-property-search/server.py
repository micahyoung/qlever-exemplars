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

Both types also have a tier-3 fallback (see resolver.py) that fires when the
first two tiers (exact lexical match, then embedding similarity) both miss:
an LLM proposes formal-name permutations of the phrase, which are then
verified deterministically -- against the local lexical index for
properties, or LIVE against QLever's rdfs:label for items, which lets item
resolution reach beyond the precomputed ~114k class universe to general
named entities (e.g. "Marie Curie", "the Big Apple"). Tier 3 is best-effort
(not guaranteed to resolve, and slower than tiers 1-2); successful
resolutions are persisted so repeat queries become instant tier-1 hits.

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
import threading

import numpy as np
import requests
from flask import Flask, request, Response

import llm_client
import qlever_client
import resolver
from index_store import (
    IndexBundle,
    append_learned_alias,
    append_learned_entity,
    load_index_bundle,
    load_learned_aliases,
    load_learned_entities,
    merge_learned_aliases,
    merge_learned_entities,
    normalize,
)

EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8888/v1/embeddings")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "qwen3-embedding-8b")
PORT = int(os.environ.get("PORT", "7002"))
MAX_LIMIT = 50
DEFAULT_LIMIT = 10

# --- tier-3 config ---------------------------------------------------------
TIER3_ENABLED = os.environ.get("TIER3_ENABLED", "true").lower() == "true"
TIER3_MIN_SCORE = float(os.environ.get("TIER3_MIN_SCORE", "0.70"))

ENTITY_PREFIX = "http://www.wikidata.org/entity/"
DIRECT_PREFIX = "http://www.wikidata.org/prop/direct/"

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_DIR = os.path.join(HERE, "index")
ITEM_INDEX_DIR = os.path.join(HERE, "index_items")

# Base files: written ONLY by build_index.py, frozen at runtime.
INDEX_META_PATH = os.path.join(INDEX_DIR, "meta.json")
ITEM_INDEX_META_PATH = os.path.join(ITEM_INDEX_DIR, "meta.json")
ITEM_INDEX_VECTORS_PATH = os.path.join(ITEM_INDEX_DIR, "vectors.npy")

# Learned-overlay files: tier-3-only, merged with the base at load time.
# Kept structurally separate from the base so "what did tier-3 add" is a
# direct file read and "undo a bad tier-3 write" never risks the base data.
LEARNED_ALIASES_PATH = os.path.join(INDEX_DIR, "learned_aliases.json")
LEARNED_ITEMS_META_PATH = os.path.join(ITEM_INDEX_DIR, "learned_entities.json")
LEARNED_ITEMS_VECTORS_PATH = os.path.join(ITEM_INDEX_DIR, "learned_vectors.npy")


# --- load indices --------------------------------------------------------------
PROPERTY_INSTRUCTION = (
    "Instruct: Given a search phrase, retrieve the Wikidata property whose "
    "meaning best matches it.\nQuery: "
)
ITEM_INSTRUCTION = (
    "Instruct: Given a search phrase, retrieve the Wikidata item (entity) whose "
    "meaning best matches it.\nQuery: "
)

_property_base = load_index_bundle(INDEX_DIR, PROPERTY_INSTRUCTION, required=True)
PROPERTY_LEARNED_ALIASES = load_learned_aliases(LEARNED_ALIASES_PATH)
PROPERTY_INDEX = merge_learned_aliases(_property_base, PROPERTY_LEARNED_ALIASES)

_item_base = load_index_bundle(ITEM_INDEX_DIR, ITEM_INSTRUCTION, required=False)
if _item_base is not None:
    ITEM_LEARNED_META, ITEM_LEARNED_VECTORS = load_learned_entities(
        LEARNED_ITEMS_META_PATH, LEARNED_ITEMS_VECTORS_PATH
    )
    ITEM_INDEX = merge_learned_entities(_item_base, ITEM_LEARNED_META, ITEM_LEARNED_VECTORS)
else:
    ITEM_LEARNED_META, ITEM_LEARNED_VECTORS = [], None
    ITEM_INDEX = None

INDEXES = {"property": PROPERTY_INDEX, "item": ITEM_INDEX}

# One lock per bundle, held only around the mutate-and-swap of a tier-3
# persistence write (never around the slow LLM/QLever calls that precede
# it), so concurrent reads are never blocked by a write in progress.
INDEX_LOCKS = {"property": threading.Lock(), "item": threading.Lock()}

app = Flask(__name__)


# --- tier-3 persistence (real implementations, injected into resolver.py) -----
def _persist_property_alias(row_index, alias):
    """Real `persist` callback for resolve_property_tier3: attaches `alias`
    to the existing property row and records it in the learned-overlay file
    so future identical queries hit tier 1 -- the base meta.json is never
    touched."""
    global PROPERTY_INDEX, PROPERTY_LEARNED_ALIASES
    new_bundle, new_learned = append_learned_alias(
        PROPERTY_INDEX, LEARNED_ALIASES_PATH, row_index, alias,
        llm_client.CHAT_MODEL, INDEX_LOCKS["property"],
    )
    PROPERTY_INDEX = new_bundle
    if new_learned is not None:  # None means the write was a no-op duplicate
        PROPERTY_LEARNED_ALIASES = new_learned
    INDEXES["property"] = new_bundle


def _persist_item_row(entity, vector):
    """Real `persist` callback for resolve_item_tier3: appends a brand-new
    entity row (never an existing one -- see resolver.py's docstring) to the
    item learned-overlay files. The base meta.json/vectors.npy are never
    touched."""
    global ITEM_INDEX, ITEM_LEARNED_META, ITEM_LEARNED_VECTORS
    new_bundle, new_meta, new_vectors = append_learned_entity(
        ITEM_INDEX, LEARNED_ITEMS_META_PATH, LEARNED_ITEMS_VECTORS_PATH,
        entity, vector, llm_client.CHAT_MODEL, INDEX_LOCKS["item"],
    )
    ITEM_INDEX = new_bundle
    if new_meta is not None:  # None means the write was a no-op duplicate
        ITEM_LEARNED_META = new_meta
        ITEM_LEARNED_VECTORS = new_vectors
    INDEXES["item"] = new_bundle

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


def search_with_score(phrase, limit, bundle):
    """Return top-`limit` meta rows from `bundle` (exact label/alias matches
    pinned first, ordered among themselves by semantic score, then pure
    semantic for the rest) alongside the raw top cosine score, as
    (hits, top_score) -- the score is needed to decide whether tier 3 should
    fire. `top_score` reflects the best PURE embedding score (before lexical
    pinning), which is fine: if a lexical pin exists, tier 3 is never even
    considered (tier 1 already succeeded)."""
    q = embed(phrase, bundle.instruction)
    scores = bundle.vectors @ q
    top_score = float(scores.max()) if len(scores) else 0.0

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
    return [bundle.meta[i] for i in result], top_score


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


def _maybe_resolve_tier3(phrase, ptype, bundle, top_score):
    """Tier-2->tier-3 routing decision. Fires only when tier 1 ALSO missed
    (an exact lexical hit means tier 1 already succeeded, making tier 3
    moot) and tier 2's top score is below TIER3_MIN_SCORE. Resolution
    failures (bad LLM output, timeouts, live-QLever errors) are caught here
    and treated as a tier-3 miss -- the caller falls back to tier-2-only
    results, response stays 200, never 500."""
    if not TIER3_ENABLED:
        return None
    if not resolver.should_attempt_tier3(phrase, bundle, top_score, TIER3_MIN_SCORE):
        return None

    try:
        if ptype == "property":
            return resolver.resolve_property_tier3(
                phrase,
                bundle,
                llm_client.generate_property_permutations,
                _persist_property_alias,
            )
        if ptype == "item":
            return resolver.resolve_item_tier3(
                phrase,
                llm_client.generate_item_permutations,
                qlever_client.resolve_entity_uri,
                qlever_client.fetch_entity_for_index,
                lambda text: embed(text, ""),
                _persist_item_row,
            )
    except Exception as exc:  # noqa: BLE001 - resolution failure -> tier-2 fallback
        print(f"WARNING: tier-3 resolution failed for {phrase!r} ({ptype}): {exc}", flush=True)
        return None
    return None


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

    try:
        hits, top_score = search_with_score(parsed["phrase"], parsed["limit"], bundle)
    except requests.exceptions.RequestException as exc:
        # The embedding call (tier 1/2, unconditional on every request) hit
        # the same shared GPU gateway tier 3 uses for chat completions --
        # under contention this can time out even for a plain embedding
        # request. Degrade to 503 rather than an unhandled 500.
        print(f"WARNING: embedding request failed for {parsed['phrase']!r}: {exc}", flush=True)
        return Response("embedding service unavailable or timed out", status=503)

    tier3_hit = _maybe_resolve_tier3(parsed["phrase"], ptype, bundle, top_score)
    if tier3_hit is not None:
        id_key = "uri"
        hits = [tier3_hit] + [h for h in hits if h[id_key] != tier3_hit[id_key]]
        hits = hits[: parsed["limit"]]

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
            "learned_property_aliases": len(PROPERTY_LEARNED_ALIASES),
            "learned_items": len(ITEM_LEARNED_META),
        }),
        content_type="application/json",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
