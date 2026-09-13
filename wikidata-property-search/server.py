#!/usr/bin/env python3
"""Property/item-search SPARQL SERVICE endpoint for QLever.

Exposes a SPARQL-shaped endpoint on :7002 whose vocabulary mirrors WDQS's
`wikibase:mwapi` so a federated query feels native to query.wikidata.org users.

Two independent indices are served, selected by `mwapi:type`:
  "property" (default) - all ~13.5k Wikidata properties
  "item"               - the ~114k entities used as the object of a wdt:P31
                          ("instance of") triple somewhere in the dataset, i.e.
                          "type/class" entities like Q5 (human), Q515 (city).

Resolution is two-tier: exact lexical match first; if that misses, an LLM
proposes formal-name candidates (see resolver.py), verified deterministically
-- against the local lexical index for properties, or LIVE against QLever's
rdfs:label for items, which lets item resolution reach beyond the precomputed
~114k class universe to general named entities (e.g. "Marie Curie", "the Big
Apple"). Tier 3 is best-effort (not guaranteed to resolve, and slower than
tier 1); successful resolutions are persisted so repeat queries become
instant tier-1 hits. If both tiers miss -- or tier 3 itself fails (LLM
error, timeout, live-verification error) -- the request raises and returns
a non-2xx response, which QLever propagates as a genuine SPARQL query
failure rather than a silent empty result. This is deliberate: QLever's own
result cache stores any HTTP-200 SERVICE response (even an empty one)
indefinitely, so a transient failure returned as a "success" would get
stuck cached as a false permanent non-match; a non-2xx response is never
cached and is retried fresh on the next identical query.

It does NOT implement general SPARQL. It recognizes a small vocabulary of
request shapes within a `SERVICE { ... }` block, parsed as a real (if
tiny) RDF graph via rdflib rather than regex, since the batched/relation
forms below need to associate several phrases each with their own type and
output variable -- something flat `bd:serviceParam` triples can't express.

Three request shapes. **A SERVICE block may contain either one legacy
single-search request, or one-or-more batch/relation requests -- never
mixed** (batch/relation requests always yield exactly one result row; the
legacy form is the only one with ranked/multi-row/labeled output, so mixing
them would create an ill-defined single-row x multi-row combination).

(a) Legacy single-search -- unchanged, for ranked/multi-row/labeled output:
    bd:serviceParam mwapi:search "date of birth" .
    bd:serviceParam mwapi:type "property" .
    bd:serviceParam wikibase:limit "5" .
    ?prop wikibase:apiOutputItem mwapi:item .
    ?label wikibase:apiOutput mwapi:label .
    ?score wikibase:apiOrdinal true .

(b) Batched independent properties/items -- one row, `mwapi:bind` is the
    uniform output predicate (binds a wdt:-direct-property URI for
    "property", an entity URI for "item"):
    [] mwapi:search "date of birth" ; mwapi:type "property" ; mwapi:bind ?birthProp .
    [] mwapi:search "date of death" ; mwapi:type "property" ; mwapi:bind ?deathProp .

(c) Relation pair -- jointly resolved and live-verified, for the
    "?x prop item" idiom (type/class checks, award/prize relations, etc. --
    anywhere the item is the DIRECT object of the property in one triple):
    [] mwapi:searchRelation "received the Nobel Prize in Physics" ;
       mwapi:bindProperty ?awardProp ;
       mwapi:bindItem ?nobelPhysics .
    Giving the LLM both halves together lets it use real disambiguating
    context (e.g. knowing the object is a WON prize, not a nomination,
    biases toward "award received" over "nominated for") -- something two
    independent, context-blind single-searches can't do. The resolver also
    live-verifies the pair as a whole (does `?s <prop> <item>` actually
    occur in the dataset?), not just that each half individually exists.
"""
import json
import os
import re
import threading
from datetime import datetime, timezone

from flask import Flask, request, Response
from rdflib import BNode, Graph, Namespace
from werkzeug.exceptions import HTTPException

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

PORT = int(os.environ.get("PORT", "7002"))
MAX_LIMIT = 50
DEFAULT_LIMIT = 10

ENTITY_PREFIX = "http://www.wikidata.org/entity/"
DIRECT_PREFIX = "http://www.wikidata.org/prop/direct/"

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_DIR = os.path.join(HERE, "index")
ITEM_INDEX_DIR = os.path.join(HERE, "index_items")

# Base files: written ONLY by build_index.py, frozen at runtime.
INDEX_META_PATH = os.path.join(INDEX_DIR, "meta.json")
ITEM_INDEX_META_PATH = os.path.join(ITEM_INDEX_DIR, "meta.json")

# Learned-overlay files: tier-3-only, merged with the base at load time.
# Kept structurally separate from the base so "what did tier-3 add" is a
# direct file read and "undo a bad tier-3 write" never risks the base data.
LEARNED_ALIASES_PATH = os.path.join(INDEX_DIR, "learned_aliases.json")
LEARNED_ITEMS_META_PATH = os.path.join(ITEM_INDEX_DIR, "learned_entities.json")
# Relation-phrase cache: phrase -> (pid, qid), distinct from the alias/item
# overlays above -- a free-text relation phrase ("received the Nobel Prize
# in Physics") won't exact-match either lexical index, so a repeat query
# needs its own shortcut straight back to the previously-verified pair.
LEARNED_RELATIONS_PATH = os.path.join(HERE, "learned_relations.json")


# --- load indices --------------------------------------------------------------
_property_base = load_index_bundle(INDEX_DIR, required=True)
PROPERTY_LEARNED_ALIASES = load_learned_aliases(LEARNED_ALIASES_PATH)
PROPERTY_INDEX = merge_learned_aliases(_property_base, PROPERTY_LEARNED_ALIASES)

_item_base = load_index_bundle(ITEM_INDEX_DIR, required=False)
if _item_base is not None:
    ITEM_LEARNED_META = load_learned_entities(LEARNED_ITEMS_META_PATH)
    ITEM_INDEX = merge_learned_entities(_item_base, ITEM_LEARNED_META)
else:
    ITEM_LEARNED_META = []
    ITEM_INDEX = None

INDEXES = {"property": PROPERTY_INDEX, "item": ITEM_INDEX}


def _load_learned_relations(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        records = json.load(f)
    return {r["phrase_norm"]: {"pid": r["pid"], "qid": r["qid"]} for r in records}


LEARNED_RELATIONS = _load_learned_relations(LEARNED_RELATIONS_PATH)

# One lock per bundle, held only around the mutate-and-swap of a tier-3
# persistence write (never around the slow LLM/QLever calls that precede
# it), so concurrent reads are never blocked by a write in progress.
INDEX_LOCKS = {"property": threading.Lock(), "item": threading.Lock(), "relation": threading.Lock()}

app = Flask(__name__)


class ResolutionError(Exception):
    """Raised whenever a phrase/relation cannot be resolved -- LLM error,
    timeout, live-verification failure, or a clean non-match after
    exhausting tier 3. Always surfaces as a non-2xx HTTP response so QLever
    propagates it as a genuine SPARQL query failure and never caches it
    (QLever's result cache only stores HTTP-200 SERVICE responses)."""


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


def _persist_item_row(entity):
    """Real `persist` callback for resolve_item_tier3: appends a brand-new
    entity row (never an existing one -- see resolver.py's docstring) to the
    item learned-overlay file. The base meta.json is never touched."""
    global ITEM_INDEX, ITEM_LEARNED_META
    new_bundle, new_meta = append_learned_entity(
        ITEM_INDEX, LEARNED_ITEMS_META_PATH, entity, llm_client.CHAT_MODEL, INDEX_LOCKS["item"],
    )
    ITEM_INDEX = new_bundle
    if new_meta is not None:  # None means the write was a no-op duplicate
        ITEM_LEARNED_META = new_meta
    INDEXES["item"] = new_bundle


def _persist_relation_cache(phrase, pid, qid):
    """Real `persist_relation_cache` callback for resolve_relation_tier3:
    records phrase -> (pid, qid) so a repeat identical relation phrase skips
    straight back to this pair instead of re-running tier 3. Does not touch
    the property alias / item row overlays -- those are separate persists,
    already applied by the time this runs."""
    global LEARNED_RELATIONS
    norm = normalize(phrase)
    with INDEX_LOCKS["relation"]:
        if norm in LEARNED_RELATIONS:  # idempotency guard, mirrors the alias/item overlays
            return
        record = {
            "phrase_norm": norm,
            "pid": pid,
            "qid": qid,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "source_model": llm_client.CHAT_MODEL,
        }
        existing = []
        if os.path.exists(LEARNED_RELATIONS_PATH):
            with open(LEARNED_RELATIONS_PATH) as f:
                existing = json.load(f)
        existing.append(record)
        tmp = LEARNED_RELATIONS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(existing, f)
        os.replace(tmp, LEARNED_RELATIONS_PATH)
        LEARNED_RELATIONS = dict(LEARNED_RELATIONS, **{norm: {"pid": pid, "qid": qid}})


# --- request parsing ----------------------------------------------------------
MWAPI = "https://www.mediawiki.org/ontology#API/"
WIKIBASE = "http://wikiba.se/ontology#"
BD = "http://www.bigdata.com/rdf#"
MWAPI_NS = Namespace(MWAPI)
WIKIBASE_NS = Namespace(WIKIBASE)
BD_NS = Namespace(BD)

_VAR_TOKEN_RE = re.compile(r"\?(\w+)")
_VAR_URN_PREFIX = "urn:var:"


def _extract_service_body(raw_query):
    """Brace-depth extraction of the graph pattern body QLever sends us.

    QLever does NOT forward the client's original `SERVICE <url> { ... }`
    wrapper -- from our endpoint's perspective it receives a normal,
    standalone query it synthesizes itself: `PREFIX ... SELECT <vars> {
    <graph pattern> }` (note: no "WHERE" keyword either, that's optional
    SPARQL syntax and QLever's serializer omits it). So we extract from the
    first "{" found anywhere in the text. If a caller instead sends a
    manually-wrapped `SERVICE <http://localhost:7002/sparql> { ... }` query
    directly to this endpoint (e.g. for direct testing without going
    through QLever), prefer extracting that inner block instead. Brace
    counting (not a single regex) is needed because the body can itself
    contain nested braces (e.g. OPTIONAL, blank-node property lists)."""
    m = re.search(r"SERVICE\s+<http://localhost:7002/sparql>\s*\{", raw_query)
    start = m.end() if m else raw_query.find("{")
    if start < 0:
        return ""
    if not m:
        start += 1  # find() points AT the brace; the SERVICE-match case points just past it
    depth = 1
    i = start
    while i < len(raw_query) and depth:
        if raw_query[i] == "{":
            depth += 1
        elif raw_query[i] == "}":
            depth -= 1
        i += 1
    return raw_query[start : i - 1]


_SELECT_VARS_RE = re.compile(r"SELECT\s+(.*?)\s*\{", re.IGNORECASE | re.DOTALL)


def _extract_expected_vars(raw_query):
    """Extract the variable names QLever's SELECT clause explicitly lists,
    in order. QLever performs a strict SET-equality check between this list
    and our response's `head.vars` (Service::verifyVariables) -- and
    critically, this list includes synthetic names QLever assigns to blank
    nodes in the graph pattern (SPARQL treats a blank node in a query as an
    anonymous variable, so it's "visible" and gets echoed into the SELECT
    clause QLever sends us, e.g. `?_QLever_internal_variable_1`), which we
    have no other way to learn since our own parser never sees QLever's
    internal AST. Returns [] for "SELECT *" or any unparseable clause (the
    case for direct/manual testing against this endpoint, where our own
    computed var list is used as a fallback instead -- see build_batched_results)."""
    m = _SELECT_VARS_RE.search(raw_query)
    if not m:
        return []
    return _VAR_TOKEN_RE.findall(m.group(1))


def _to_turtle(body):
    """Rewrite `?var` tokens to placeholder IRIs (SPARQL variables aren't
    valid Turtle terms) and wrap with prefixes so the SERVICE body parses as
    plain Turtle -- the request shapes below (ground triples + string
    literals + blank-node property lists) are a syntactic subset of Turtle
    once variables are swapped out."""
    body = _VAR_TOKEN_RE.sub(lambda m: f"<{_VAR_URN_PREFIX}{m.group(1)}>", body)
    return (
        f"@prefix mwapi: <{MWAPI}> .\n"
        f"@prefix wikibase: <{WIKIBASE}> .\n"
        f"@prefix bd: <{BD}> .\n"
        + body
    )


def _varname(node):
    """<urn:var:X> -> "X"; anything else -> None."""
    s = str(node)
    return s[len(_VAR_URN_PREFIX) :] if s.startswith(_VAR_URN_PREFIX) else None


class ParsedService:
    def __init__(self):
        self.legacy = None  # dict, the single-search request, or None
        self.batches = []  # list of {"phrase", "type", "bind_var"}
        self.relations = []  # list of {"phrase", "property_var", "item_var"}
        self.expected_vars = []  # QLever's exact SELECT-clause var list, if known


def parse_service_body(raw_query):
    """Parse a SERVICE block into a ParsedService covering all three
    request shapes. Returns an empty ParsedService (no legacy/batches/
    relations) if the block is missing or has no recognized triples."""
    parsed = ParsedService()
    parsed.expected_vars = _extract_expected_vars(raw_query)
    body = _extract_service_body(raw_query)
    if not body.strip():
        return parsed

    graph = Graph()
    graph.parse(data=_to_turtle(body), format="turtle")

    # Legacy single-search uses a fixed, repeated `bd:serviceParam` IRI as
    # the subject of every param triple -- NOT a blank node -- so batch/
    # relation requests (shape b/c) are distinguished by being scoped to an
    # actual blank node subject, one per request.
    batch_subjects = {s for s in graph.subjects(MWAPI_NS.search, None) if isinstance(s, BNode)}
    relation_subjects = {s for s in graph.subjects(MWAPI_NS.searchRelation, None) if isinstance(s, BNode)}

    search_vals = [
        o for s, o in graph.subject_objects(MWAPI_NS.search) if s not in batch_subjects
    ]
    if search_vals:
        phrase = str(search_vals[0])
        type_vals = [
            o for s, o in graph.subject_objects(MWAPI_NS.type) if s not in batch_subjects
        ]
        ptype = str(type_vals[0]) if type_vals else None
        limit_vals = [
            o for s, o in graph.subject_objects(WIKIBASE_NS.limit) if s not in batch_subjects
        ]
        try:
            limit = int(str(limit_vals[0])) if limit_vals else DEFAULT_LIMIT
        except ValueError:
            limit = DEFAULT_LIMIT
        limit = max(1, min(limit, MAX_LIMIT))

        item_var = direct_var = label_var = score_var = None
        for s, o in graph.subject_objects(WIKIBASE_NS.apiOutputItem):
            if o == MWAPI_NS.item:
                item_var = _varname(s)
        for s, o in graph.subject_objects(WIKIBASE_NS.apiOutput):
            if o == MWAPI_NS.directProperty:
                direct_var = _varname(s)
            elif o == MWAPI_NS.label:
                label_var = _varname(s)
        for s, o in graph.subject_objects(WIKIBASE_NS.apiOrdinal):
            if str(o).lower() == "true":
                score_var = _varname(s)

        parsed.legacy = {
            "phrase": phrase,
            "type": ptype,
            "limit": limit,
            "item_var": item_var,
            "direct_var": direct_var,
            "label_var": label_var,
            "score_var": score_var,
        }

    for subj in batch_subjects:
        phrase = str(graph.value(subj, MWAPI_NS.search))
        ptype = graph.value(subj, MWAPI_NS.type)
        ptype = str(ptype) if ptype is not None else "property"
        bind = graph.value(subj, MWAPI_NS.bind)
        bind_var = _varname(bind) if bind is not None else None
        if bind_var:
            parsed.batches.append({"phrase": phrase, "type": ptype, "bind_var": bind_var})

    for subj in relation_subjects:
        phrase = str(graph.value(subj, MWAPI_NS.searchRelation))
        prop_node = graph.value(subj, MWAPI_NS.bindProperty)
        item_node = graph.value(subj, MWAPI_NS.bindItem)
        property_var = _varname(prop_node) if prop_node is not None else None
        item_var = _varname(item_node) if item_node is not None else None
        if property_var and item_var:
            parsed.relations.append({"phrase": phrase, "property_var": property_var, "item_var": item_var})

    return parsed


# --- search -------------------------------------------------------------------
def search_lexical(phrase, limit, bundle, ptype=None):
    """Return up to `limit` exact lexical hits for phrase. Rows are already
    ordered label-match-before-alias-match (index_store._index_rows), which
    resolves ties between two DIFFERENT rows' label/alias -- but it can't
    help when MULTIPLE rows share the exact same label (e.g. "city" is the
    literal label of Q515 "large human settlement" AND of several
    country-specific settlement-type items). That's common for items (item
    labels aren't unique the way property labels are), so for ptype=="item"
    with more than one exact match, break the tie live via the same
    notability signal tier-3 already uses for the same purpose
    (qlever_client.rank_by_local_notability) -- a live round trip only when
    there's genuine ambiguity, not on the common single-match path."""
    rows = bundle.lex_index.get(normalize(phrase), [])
    if ptype == "item" and len(rows) > 1:
        uris = [bundle.meta[i]["uri"] for i in rows]
        winner_uri = qlever_client.rank_by_local_notability(uris)
        rows = sorted(rows, key=lambda i: bundle.meta[i]["uri"] != winner_uri)
    return [bundle.meta[i] for i in rows[:limit]]


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


def _empty_results(expected_vars=None):
    return Response(
        json.dumps({"head": {"vars": expected_vars or []}, "results": {"bindings": []}}),
        content_type="application/sparql-results+json",
    )


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


def _resolve_tier3(phrase, ptype, bundle):
    """Tier-3 LLM-backed resolution. Raises ResolutionError on any failure
    to resolve. LLM/timeout/live-verification exceptions from resolver.py
    propagate as-is (its contract already raises on real failures and
    returns None only for a clean non-match); a None return is turned into
    a ResolutionError here so every failure mode looks the same to the
    caller and to the SPARQL client."""
    if ptype == "property":
        hit = resolver.resolve_property_tier3(
            phrase,
            bundle,
            llm_client.generate_property_permutations,
            _persist_property_alias,
        )
    elif ptype == "item":
        hit = resolver.resolve_item_tier3(
            phrase,
            llm_client.generate_item_permutations,
            qlever_client.resolve_entity_uri,
            qlever_client.fetch_entity_for_index,
            _persist_item_row,
        )
    else:
        hit = None
    if hit is None:
        raise ResolutionError(f"no resolution for {phrase!r} ({ptype})")
    return hit


def _resolve_one(phrase, ptype):
    """Tier-1-then-tier-3 resolution of a single phrase to its best (only)
    hit. Used by the legacy form's single-hit paths and by each entry of a
    batched-independent-properties request. Raises ResolutionError if
    neither tier resolves it."""
    if ptype not in INDEXES or INDEXES[ptype] is None:
        raise ResolutionError(f'unsupported or unloaded mwapi:type "{ptype}"')
    bundle = INDEXES[ptype]
    hits = search_lexical(phrase, 1, bundle, ptype=ptype)
    if hits:
        return hits[0]
    return _resolve_tier3(phrase, ptype, bundle)


def _resolve_relation(phrase):
    """Tier-1 relation-phrase-cache lookup, else tier-3 joint resolution.
    Returns (property_meta, item_meta) on success; raises ResolutionError
    otherwise."""
    cached = LEARNED_RELATIONS.get(normalize(phrase))
    if cached:
        prop_meta = next((m for m in PROPERTY_INDEX.meta if m.get("pid") == cached["pid"]), None)
        item_meta = None
        if ITEM_INDEX is not None:
            item_meta = next((m for m in ITEM_INDEX.meta if m.get("qid") == cached["qid"]), None)
        if prop_meta and item_meta:
            return prop_meta, item_meta

    if ITEM_INDEX is None:
        raise ResolutionError("item index is not loaded; cannot resolve relations")

    prop_meta, item_meta = resolver.resolve_relation_tier3(
        phrase,
        llm_client.generate_relation_pairs,
        PROPERTY_INDEX,
        ITEM_INDEX,
        qlever_client.resolve_entity_uri,
        qlever_client.fetch_entity_for_index,
        qlever_client.triple_exists,
        _persist_property_alias,
        _persist_item_row,
        _persist_relation_cache,
    )
    if prop_meta is None or item_meta is None:
        raise ResolutionError(f"no relation resolution for {phrase!r}")
    return prop_meta, item_meta


def build_batched_results(parsed):
    """Resolve every batch/relation request in `parsed` into a single
    merged row. Atomic: if any single sub-lookup fails to resolve, it
    raises ResolutionError (propagated to the caller) and the whole SERVICE
    call fails -- rather than silently leaving that variable unbound, which
    would otherwise risk an unconstrained-predicate/object scan downstream
    once the resolved-but-partial row feeds into the composed query.

    `head.vars` mirrors QLever's exact expected variable list when known
    (parsed.expected_vars) rather than just the vars our own requests bind --
    QLever's Service::verifyVariables requires an exact SET match, and that
    expected set includes synthetic names for blank-node "requests" in the
    original query (see _extract_expected_vars). Falls back to our own
    computed var list only when expected_vars is unavailable (e.g. a query
    sent directly to this endpoint for manual testing, not via QLever)."""
    row = {}
    vars_ = []

    for req in parsed.batches:
        vars_.append(req["bind_var"])
        hit = _resolve_one(req["phrase"], req["type"])
        uri = hit["uri"]
        if req["type"] == "property":
            uri = uri.replace(ENTITY_PREFIX, DIRECT_PREFIX)
        row[req["bind_var"]] = {"type": "uri", "value": uri}

    for req in parsed.relations:
        vars_.extend([req["property_var"], req["item_var"]])
        prop_hit, item_hit = _resolve_relation(req["phrase"])
        row[req["property_var"]] = {
            "type": "uri", "value": prop_hit["uri"].replace(ENTITY_PREFIX, DIRECT_PREFIX),
        }
        row[req["item_var"]] = {"type": "uri", "value": item_hit["uri"]}

    head_vars = parsed.expected_vars if parsed.expected_vars else vars_
    return {"head": {"vars": head_vars}, "results": {"bindings": [row]}}


@app.route("/sparql", methods=["GET", "POST"])
def sparql():
    query = get_query()
    parsed = parse_service_body(query)

    if parsed.legacy and (parsed.batches or parsed.relations):
        return Response(
            "a SERVICE block may contain either one legacy mwapi:search "
            "request or one-or-more batch/mwapi:searchRelation requests, "
            "not both",
            status=400,
        )

    if parsed.batches or parsed.relations:
        out = build_batched_results(parsed)
        return Response(json.dumps(out), content_type="application/sparql-results+json")

    if not parsed.legacy or not parsed.legacy["phrase"]:
        return _empty_results(parsed.expected_vars)

    legacy = parsed.legacy
    ptype = legacy["type"] or "property"  # default when mwapi:type is omitted
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

    hits = search_lexical(legacy["phrase"], legacy["limit"], bundle, ptype=ptype)
    if not hits:
        hits = [_resolve_tier3(legacy["phrase"], ptype, bundle)]

    out = build_results(legacy, hits)
    # Mirror QLever's exact expected variable set when known (see
    # _extract_expected_vars) rather than just the vars this legacy request
    # itself binds -- verifyVariables requires an exact match.
    if parsed.expected_vars:
        out["head"]["vars"] = parsed.expected_vars
    return Response(json.dumps(out), content_type="application/sparql-results+json")


@app.errorhandler(Exception)
def handle_resolution_failure(exc):
    """Turn any propagated resolution failure (ResolutionError, or a stray
    LLM/live-QLever exception like a timeout) into a uniform non-2xx
    response, while leaving Flask's own routing errors (404, 405, ...)
    untouched. QLever never caches a non-2xx SERVICE response, so this is
    what lets a retry actually re-attempt resolution instead of replaying a
    stale cached "no match" from a transient failure."""
    if isinstance(exc, HTTPException):
        return exc
    print(f"WARNING: resolution failed: {exc}", flush=True)
    return Response(str(exc), status=502)


@app.route("/", methods=["GET"])
def health():
    return Response(
        json.dumps({
            "status": "ok",
            "properties": len(PROPERTY_INDEX.meta),
            "items": len(ITEM_INDEX.meta) if ITEM_INDEX else 0,
            "item_index_loaded": ITEM_INDEX is not None,
            "learned_property_aliases": len(PROPERTY_LEARNED_ALIASES),
            "learned_items": len(ITEM_LEARNED_META),
            "learned_relations": len(LEARNED_RELATIONS),
        }),
        content_type="application/json",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
