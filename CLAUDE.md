# QLever Wikidata Truthy

## Overview

This directory hosts a local [QLever](https://docs.qlever.dev/) SPARQL endpoint over the **Wikidata truthy** dataset — only statements ranked "truthy" (~8.2B triples). It's useful for querying a curated, high-confidence subset of Wikidata without the noise of conflicting or deprecated statements.

### Structure

```
qlever/
├── CLAUDE.md                    # this file
├── qlever.sources               # deb822 apt source (system-wide)
├── wikidata-truthy/             # self-contained dataset (port 7001)
│   ├── Qleverfile               # QLever INI-style config
│   ├── latest-truthy.nt.bz2     # 40 GB source dump
│   ├── wikidata-truthy.*        # index, vocabulary, and log files
│   └── wikidata-truthy.internal.index.*
├── wikidata-property-search/    # property/item-search SERVICE (port 7002)
│   ├── build_index.py           # fetches property and item metadata into both indices below
│   ├── server.py                # mwapi-style SPARQL SERVICE endpoint
│   ├── index/                   # property index: meta.json
│   └── index_items/             # item (class/type) index: meta.json
└── yago-4/                      # self-contained dataset (port 9004)
    ├── Qleverfile               # QLever INI-style config
    ├── yago-4.6-*.zip           # 6 source dump files, ~10 GB total
    └── yago-4.*                 # index, vocabulary, and log files
```

Additional datasets can be added as sibling directories (e.g., `another-dataset/`), each with its own `Qleverfile` and index, running independently on separate ports.

## Building the Index

From inside `wikidata-truthy/`:

1. **Install QLever** — `sudo apt update && sudo apt install qlever`
2. **Create Qleverfile** — see the existing one as a template (points to `latest-truthy.nt.bz2`)
3. **Download data** — `qlever get-data`
4. **Build index** — `qlever index` (~5–6 hours, ~500 GB disk, uses `pbzip2 -dc` for parallel decompression)
5. **Start server** — `qlever start`

If the dump already exists, `qlever get-data` will resume or skip it. If re-indexing, clean old artifacts first:
```bash
rm -f wikidata-truthy.* wikidata-truthy.internal.index.*
```

## Querying

**SERVICE is the default for resolving predicates from natural language.**
Never probe predicate frequencies to discover properties — SERVICE handles exact matches (pinned first, and preferred over any looser alias match — see below) for `mwapi:type "property"`.

**SERVICE can attempt general entity resolution, but it's best-effort.** `mwapi:type "item"` indexes the ~114k entities that appear as the object of a `P31` ("instance of") triple somewhere in the dataset — i.e. class/type entities like `Q5` (human) or `Q515` (city) — and resolves those instantly. For everything else (e.g. "Marie Curie", "the Big Apple"), it falls back to a tier-3 resolver: an LLM proposes candidate canonical names, each is verified with a **live** exact-match query against QLever itself (not just the precomputed index), and same-labeled candidates are disambiguated by statement count. This is not guaranteed to resolve, and has LLM-round-trip latency (can be several seconds to tens of seconds) on a cache miss — successful resolutions are persisted, so a repeat query for the same phrase becomes an instant exact-match hit.

**A failed resolution — LLM error, timeout, live-verification error, or a genuine non-match — surfaces as a real SPARQL query failure on the outer query, not a silent empty result.** This is deliberate: QLever's own result cache stores any HTTP-200 SERVICE response indefinitely, even an empty-bindings one, so returning "success, zero rows" on a transient failure would get that failure stuck cached as a false permanent non-match. A non-2xx response from the SERVICE endpoint is never cached and is retried fresh on the next identical query. Practical implications: a required (non-`OPTIONAL`) `SERVICE` block that fails to resolve fails the *whole* composed query — expect and handle a query-level error (not zero rows) when a phrase doesn't resolve, and fall back to the manual `rdfs:label` query below rather than assuming an empty result means "no data." A batched (`mwapi:bind`) call with several phrases in one `SERVICE` block also fails atomically: if any one sub-lookup fails, the entire call fails rather than coming back with only some variables bound.

If you want guaranteed, fast, manual control over entity resolution instead (or SERVICE/tier-3 doesn't resolve something), use a direct `rdfs:label` **exact-match** query yourself:

```sparql
SELECT ?item ?label WHERE {
  ?item rdfs:label "Gweru"@en .
  BIND("Gweru"@en AS ?label)
} LIMIT 10
```

Never use `FILTER(CONTAINS(?label, "..."))` (or any other unanchored label scan) — it forces a full unindexed scan over the label predicate and will time out, the same way an unconstrained variable-predicate scan does. Use the exact literal match above, then disambiguate candidates (there are often several) with a follow-up query constraining on `P31`/`P131` etc.

### Step 1: Resolve

Use SERVICE to resolve every predicate from the prompt. For named entities, a single `mwapi:type "item"` SERVICE call may now resolve common ones directly (via the tier-3 fallback described above); the direct `rdfs:label` exact-match query remains the fast, deterministic, manual fallback — reach for it when you want guaranteed disambiguation control, or when SERVICE/tier-3 doesn't resolve something. This is typically 2 queries up front (one SERVICE call can batch multiple predicates; a manual entity lookup, when needed, is separate).

**Prefer `mwapi:searchRelation` for `?x P Q`-shaped idioms** — anywhere a resolved property and a resolved item are the predicate and direct object of the *same* triple (type/class checks like "instance of X", award/prize relations, etc.). Resolving them jointly gives the LLM real disambiguating context a plain independent lookup can't (e.g. knowing the object is a *won* prize, not a nomination, biases toward "award received" over "nominated for"), and the resolver additionally live-verifies that the resolved triple actually occurs in the dataset — not just that each half individually exists. See the syntax reference below.

```sparql
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX mwapi:    <https://www.mediawiki.org/ontology#API/>
PREFIX bd:       <http://www.bigdata.com/rdf#>

SELECT ?prop ?propLabel ?propScore ?entity ?entityLabel ?entityScore WHERE {
  # Resolve the predicate
  SERVICE <http://localhost:7002/sparql> {
    bd:serviceParam mwapi:search "date of incorporation" .
    bd:serviceParam mwapi:type "property" .
    bd:serviceParam wikibase:limit "1" .
    ?prop wikibase:apiOutputItem mwapi:item .
    ?propLabel wikibase:apiOutput mwapi:label .
    ?propScore wikibase:apiOrdinal true .
  }
}
```

### Step 2: Compose

Plug resolved P-id and Q-id into the final query. Use `*` (zero-or-more) on `P131` — administrative hierarchies are rarely flat. **Always constrain the subject** (a `VALUES` list or a `P31` type filter); an unconstrained variable-predicate scan can take 90+ seconds and crash QLever.

```sparql
PREFIX wd:  <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?city ?label ?incorporationDate
WHERE {
  ?city wdt:P10786 ?incorporationDate .
  ?city wdt:P131* wd:Q109681 .
  ?city rdfs:label ?label .
  FILTER(LANG(?label) = "en")
}
ORDER BY ?incorporationDate ?label
```

Typically **2–3 queries**: 1 SERVICE call to resolve predicates, 1 `rdfs:label` exact-match to resolve each named entity (occasionally a follow-up to disambiguate multiple candidates), then 1 compose.

---

### SERVICE syntax reference

Both servers must be running: QLever on `:7001`, property-search on `:7002`.

There are three request shapes. **A single `SERVICE { ... }` block may contain either one legacy `mwapi:search` request, or one-or-more batch/`mwapi:searchRelation` requests — never mixed** (batch/relation requests always resolve to exactly one row; the legacy form is the only one with ranked/multi-row/labeled output).

**(a) Legacy single-search — ranked list, multi-row, labeled output:**

```sparql
SELECT ?property ?label ?score WHERE {
  SERVICE <http://localhost:7002/sparql> {
    bd:serviceParam mwapi:search "inception" .
    bd:serviceParam mwapi:type "property" .
    bd:serviceParam wikibase:limit "5" .
    ?property wikibase:apiOutputItem mwapi:item .
    ?label wikibase:apiOutput mwapi:label .
    ?score wikibase:apiOrdinal true .
  }
} ORDER BY ?score
```

**(b) Batched independent properties/items — one row, `mwapi:bind` is the uniform output predicate** (binds a `wdt:`-direct-property URI for `"property"`, an entity URI for `"item"`). Use this to resolve several unrelated phrases in one round trip:

```sparql
SELECT ?item ?itemLabel ?birth ?death WHERE {
  SERVICE <http://localhost:7002/sparql> {
    [] mwapi:search "date of birth" ; mwapi:type "property" ; mwapi:bind ?birthProp .
    [] mwapi:search "date of death" ; mwapi:type "property" ; mwapi:bind ?deathProp .
  }
  VALUES ?item { wd:Q937 wd:Q762 }
  ?item ?birthProp ?birth .
  OPTIONAL { ?item ?deathProp ?death }
  ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel)="en")
}
```

**(c) Relation pair — jointly resolved and live-verified,** for the `?x P Q` idiom described in Step 1 above:

```sparql
SELECT ?person ?personLabel WHERE {
  SERVICE <http://localhost:7002/sparql> {
    [] mwapi:searchRelation "received the Nobel Prize in Physics" ;
       mwapi:bindProperty ?awardProp ;
       mwapi:bindItem ?nobelPhysics .
  }
  ?person ?awardProp ?nobelPhysics .
  ?person rdfs:label ?personLabel . FILTER(LANG(?personLabel)="en")
}
```

⚠️ **If a relation pair's two output variables both feed the same triple (as above), and that triple's subject isn't otherwise constrained elsewhere in the query, issue the relation lookup as two separate identical `SERVICE` calls, one per output variable** (see `exemplars.ttl`'s CQ4/CQ6 for worked examples) — binding both from a single `SERVICE` result into one unconstrained-subject triple makes QLever's planner choose a catastrophic plan (multi-GB allocation attempt or 60+ second hang). The second identical call is cheap: successful relation resolutions are cached by phrase, so it's a near-instant repeat lookup, not a second LLM round trip.

```sparql
SELECT ?person ?personLabel WHERE {
  SERVICE <http://localhost:7002/sparql> {
    [] mwapi:searchRelation "received the Nobel Prize in Physics" ;
       mwapi:bindProperty ?awardProp ;
       mwapi:bindItem ?nobelPhysicsA .
  }
  SERVICE <http://localhost:7002/sparql> {
    [] mwapi:searchRelation "received the Nobel Prize in Physics" ;
       mwapi:bindProperty ?awardPropB ;
       mwapi:bindItem ?nobelPhysics .
  }
  ?person ?awardProp ?nobelPhysics .
  ?person rdfs:label ?personLabel . FILTER(LANG(?personLabel)="en")
}
```

⚠️ **A second, distinct hazard: a SERVICE-bound-predicate triple whose object joins into a plain (non-SERVICE-bound) triple** — e.g. resolving a property, then looking up the resolved object's `rdfs:label` — **can make QLever plan it as a full-dataset unbound-predicate scan**, deferring the known predicate-equality filter until after joining against the other large scan. Not specific to `OPTIONAL`. Fix: isolate the SERVICE-bound-predicate triple alone in its own subquery: `{ SELECT ?var1 ?var2 WHERE { SERVICE {...} ; ?var1 ?resolvedProp ?var2 . } }`. See `exemplars.ttl`'s CQ9. Not universal — a triple whose only downstream constraint is a range/numeric `FILTER` (not another join) doesn't benefit; see CQ3.

⚠️ **A third hazard: a SERVICE-bound-predicate triple used inside `FILTER NOT EXISTS`/`MINUS`, or inside a `UNION` over both subject/object direction (for a symmetric relation like spouse), triggers the same unbound-predicate-object scan — and CQ9's subquery-isolation fix does NOT help here.** Isolation only helps when the isolated triple feeds a further *join*; when it's instead the operand of a set-difference or one branch of a direction-`UNION`, QLever still can't defer choosing an index-scan pattern until the SERVICE result materializes (confirmed: both forms hit a 65 GB allocation attempt / 30s timeout even fully isolated). No safe single-query SERVICE composition is known for either shape. Fix: resolve the property via a separate SERVICE call first, then hardcode the returned IRI into the negation/`UNION` clause of a second query. See `exemplars.ttl`'s CQ10 (negation) and CQ11 (symmetric relation).

⚠️ **A fourth hazard, more severe than the first: two *different* `mwapi:searchRelation` pairs, each individually split into per-variable calls per the first hazard's fix, still time out if their triples are joined only by a shared unconstrained subject** (e.g. "people who received both award X and award Y" — two independent relation lookups joined on `?person`). The per-variable split only helps within one relation's own two output variables, not across two unrelated relations sharing a subject; confirmed this still produces a 30s timeout ("Join on ?awardProp") even with both relations split. Fix: don't resolve either side as a joint property+item pair — resolve the *shared* property once via plain `mwapi:search`/`mwapi:bind`, and resolve each item via a direct `rdfs:label` exact-match instead of `mwapi:searchRelation`. See `exemplars.ttl`'s CQ12.

**Vocabulary:**

| Input | Meaning |
|---|---|
| `mwapi:search "<phrase>"` | search phrase, legacy/batch forms (required) |
| `mwapi:searchRelation "<phrase>"` | search phrase for a joint property+item relation, relation form (required) |
| `mwapi:type "property"` \| `"item"` | `property` (default) resolves predicates; `item` resolves the class/type entities described above instantly, plus general named entities on a best-effort basis via the tier-3 live-verification fallback. Not used by the relation form (it always resolves one property + one item together) |
| `wikibase:limit "N"` | max results, legacy form only, default 10, capped at 50 |

| Output binding | Returns | Forms |
|---|---|---|
| `?p wikibase:apiOutputItem mwapi:item` | entity URI `.../entity/Pxxx` or `.../entity/Qxxx` (carries labels) | legacy |
| `?p wikibase:apiOutput mwapi:directProperty` | predicate URI `.../prop/direct/Pxxx` (use as `?prop` in `?s ?p ?o`); only emitted for `mwapi:type "property"`, never for `"item"` | legacy |
| `?l wikibase:apiOutput mwapi:label` | English label | legacy |
| `?s wikibase:apiOrdinal true` | 1-based rank | legacy |
| `?v mwapi:bind` | predicate URI (property) or entity URI (item), one row only | batch |
| `?v mwapi:bindProperty` | predicate URI, one row only | relation |
| `?v mwapi:bindItem` | entity URI, one row only | relation |

Resolution is two-tier: exact lexical match first (label matches are always preferred over alias matches when a phrase ties across rows; live statement-count notability breaks ties between multiple items sharing an identical label); if that misses, the LLM-proposed-and-verified tier-3 fallback described above.

### Start/stop the server

```bash
cd wikidata-truthy
qlever start    # runs on port 7001
qlever stop     # stop when done
```

QLever's general result cache (`CACHE_MAX_SIZE` in the same `[server]` section) caches *any* HTTP-200 SERVICE response, success or empty alike, keyed on the literal query text — this is why property-search surfaces resolution failures as non-2xx responses rather than empty-but-200 ones (see the tier-3 paragraph above): only a non-2xx response is guaranteed to be retried fresh instead of replaying a stale cached miss.

### SPARQL API (HTTP)

```bash
curl -s --max-time 30 localhost:7001 \
  --data-urlencode 'query=SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 5' \
  -H 'Accept: application/sparql-results+json'
```

### Query timeout

`wikidata-truthy/Qleverfile`'s `[server]` section sets `TIMEOUT = 30s` — QLever's own
compiled-in default, deliberately *not* raised, so a pathological query plan (e.g. an
unconstrained variable-predicate scan, or a SERVICE-bound-predicate join that defeats the
query planner — see the multi-variable SERVICE join note above) fails fast instead of
grinding for minutes and burning the shared memory/cache budget.

Override per-request with the `timeout` URL parameter — always allowed to go *lower* than
the server default with no auth, but going *higher* requires `access-token`:

```bash
# lower — no access token needed
curl -s localhost:7001 --data-urlencode 'query=...' --data-urlencode 'timeout=5s'

# higher — needs the access token
curl -s localhost:7001 --data-urlencode 'query=...' \
  --data-urlencode 'timeout=90s' --data-urlencode 'access-token=wikidata-truthy'
```

This matters for composed queries that rely on the property-search SERVICE's tier-3
LLM fallback (see above) — a cache-miss resolution round trip can itself take "several
seconds to tens of seconds," which combined with the outer query's own execution time can
exceed the 30s default. If you expect a query to legitimately need longer than 30s (a
tier-3 cache miss, or a large aggregation), pass a higher `timeout` explicitly rather than
relying on the server-wide default.
