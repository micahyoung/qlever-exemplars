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
├── wikidata-property-search/    # semantic property/item-search SERVICE (port 7002)
│   ├── build_index.py           # embeds property and item text into both indices below
│   ├── server.py                # mwapi-style SPARQL SERVICE endpoint
│   ├── index/                   # property index: vectors.npy + meta.json
│   └── index_items/             # item (class/type) index: vectors.npy + meta.json
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
Never probe predicate frequencies to discover properties — SERVICE handles exact matches (pinned first) and semantic similarity for `mwapi:type "property"`.

**SERVICE can attempt general entity resolution, but it's best-effort.** `mwapi:type "item"` indexes the ~114k entities that appear as the object of a `P31` ("instance of") triple somewhere in the dataset — i.e. class/type entities like `Q5` (human) or `Q515` (city) — and resolves those instantly. For everything else (e.g. "Marie Curie", "the Big Apple"), it now falls back to a tier-3 resolver: an LLM proposes candidate canonical names, each is verified with a **live** exact-match query against QLever itself (not just the precomputed index), and same-labeled candidates are disambiguated by statement count. This is not guaranteed to resolve, and has LLM-round-trip latency (can be several seconds to tens of seconds) on a cache miss — successful resolutions are persisted, so a repeat query for the same phrase becomes an instant exact-match hit. If you want guaranteed, fast, manual control over entity resolution instead (or SERVICE/tier-3 is unavailable), use a direct `rdfs:label` **exact-match** query yourself:

```sparql
SELECT ?item ?label WHERE {
  ?item rdfs:label "Gweru"@en .
  BIND("Gweru"@en AS ?label)
} LIMIT 10
```

Never use `FILTER(CONTAINS(?label, "..."))` (or any other unanchored label scan) — it forces a full unindexed scan over the label predicate and will time out, the same way an unconstrained variable-predicate scan does. Use the exact literal match above, then disambiguate candidates (there are often several) with a follow-up query constraining on `P31`/`P131` etc.

### Step 1: Resolve

Use SERVICE to resolve every predicate from the prompt. For named entities, a single `mwapi:type "item"` SERVICE call may now resolve common ones directly (via the tier-3 fallback described above); the direct `rdfs:label` exact-match query remains the fast, deterministic, manual fallback — reach for it when you want guaranteed disambiguation control, or when SERVICE/tier-3 doesn't resolve something. This is typically 2 queries up front (one SERVICE call can batch multiple predicates; a manual entity lookup, when needed, is separate).

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

**Resolve a property (ranked list):**

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

**One-shot: resolve and query in a single SPARQL** — bind `mwapi:directProperty` (the `wdt:` form) straight into predicate position:

```sparql
SELECT ?item ?itemLabel ?val WHERE {
  SERVICE <http://localhost:7002/sparql> {
    bd:serviceParam mwapi:search "date of birth" .
    bd:serviceParam mwapi:type "property" .
    bd:serviceParam wikibase:limit "1" .
    ?prop wikibase:apiOutput mwapi:directProperty .
  }
  VALUES ?item { wd:Q937 wd:Q762 }
  ?item ?prop ?val .
  ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel)="en")
}
```

**Vocabulary:**

| Input | Meaning |
|---|---|
| `mwapi:search "<phrase>"` | search phrase (required) |
| `mwapi:type "property"` \| `"item"` | `property` (default) resolves predicates; `item` resolves the class/type entities described above instantly, plus general named entities on a best-effort basis via the tier-3 live-verification fallback |
| `wikibase:limit "N"` | max results, default 10, capped at 50 |

| Output binding | Returns |
|---|---|
| `?p wikibase:apiOutputItem mwapi:item` | entity URI `.../entity/Pxxx` or `.../entity/Qxxx` (carries labels) |
| `?p wikibase:apiOutput mwapi:directProperty` | predicate URI `.../prop/direct/Pxxx` (use as `?prop` in `?s ?p ?o`); only emitted for `mwapi:type "property"`, never for `"item"` |
| `?l wikibase:apiOutput mwapi:label` | English label |
| `?s wikibase:apiOrdinal true` | 1-based rank |

### Start/stop the server

```bash
cd wikidata-truthy
qlever start    # runs on port 7001
qlever stop     # stop when done
```

### SPARQL API (HTTP)

```bash
curl -s --max-time 30 localhost:7001 \
  --data-urlencode 'query=SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 5' \
  -H 'Accept: application/sparql-results+json'
```
