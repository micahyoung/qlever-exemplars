# Wikidata Property Semantic Search (SPARQL `SERVICE`)

A small, offline service that turns a **natural-language phrase into ranked Wikidata
property P-ids** by semantic similarity, callable inline from the local QLever endpoint
via SPARQL `SERVICE`. Its vocabulary mirrors query.wikidata.org's `wikibase:mwapi` so
federated queries feel native.

Unlike Wikidata's REST/`wbsearchentities` search (lexical, label/alias only — `"inception"`
returns just `P571`), this ranks by *meaning*, so `"inception"` also surfaces semantically
adjacent properties (`P580` start time, `P575` time of discovery, …).

**Ranking is hybrid:** an exact (case/whitespace-normalized) match on a property's label or
alias is pinned to the top; everything else is ranked by pure embedding similarity. This keeps
the semantic fan-out for conceptual phrases while guaranteeing that a phrase you *know* is a
property name (e.g. `"date of birth"` → `P569`) lands first rather than losing to a near-synonym
whose description happens to quote the phrase (`P3150` birthday).

## How it works

- `build_index.py` pulls all ~13.5k properties (label + description + aliases) from QLever
  (`:7001`), embeds a composed text per property via the local embedding server
  (`:8888`, `qwen3-embedding-8b`, 4096-dim), and saves `index/vectors.npy` + `index/meta.json`.
- `server.py` loads those vectors and serves a SPARQL-shaped endpoint on `:7002`. Per request
  it embeds the search phrase, does a brute-force cosine top-k (sub-10ms), and returns
  SPARQL-results JSON.

## Setup

```bash
cd wikidata-property-search
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# QLever (:7001) and the embedding server (:8888) must be running.
.venv/bin/python build_index.py        # one-time; re-run after re-indexing truthy
.venv/bin/python server.py             # serves on :7002 (foreground)
```

## Querying

Both `:7001` (QLever) and `:7002` (this service) must be running.

**List ranked properties for a phrase:**

```sparql
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX mwapi:    <https://www.mediawiki.org/ontology#API/>
PREFIX bd:       <http://www.bigdata.com/rdf#>

SELECT ?property ?label ?score WHERE {
  SERVICE <http://localhost:7002/sparql> {
    bd:serviceParam mwapi:search   "inception" .
    bd:serviceParam mwapi:type     "property" .
    bd:serviceParam wikibase:limit "10" .
    ?property wikibase:apiOutputItem mwapi:item .
    ?label    wikibase:apiOutput     mwapi:label .
    ?score    wikibase:apiOrdinal    true .
  }
} ORDER BY ?score
```

**Single query, no explicit PID** — resolve the property *and* query truthy data at once.
Use `mwapi:directProperty` (the `wdt:` form) so the result is usable as a predicate:

```sparql
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX mwapi:    <https://www.mediawiki.org/ontology#API/>
PREFIX bd:       <http://www.bigdata.com/rdf#>
PREFIX rdfs:     <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?item ?itemLabel ?date WHERE {
  SERVICE <http://localhost:7002/sparql> {
    bd:serviceParam mwapi:search   "inception" .
    bd:serviceParam mwapi:type     "property" .
    bd:serviceParam wikibase:limit "1" .
    ?prop wikibase:apiOutput mwapi:directProperty .   # -> wdt:P571
  }
  ?item ?prop ?date .
  ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel)="en")
} LIMIT 20
```

> **Important — constrain the subject.** When the resolved `?prop` is used as a predicate,
> **always bind `?item` to a small set first** (a `VALUES` list, or `?item wdt:P31 <class>`,
> etc.). With a constrained subject these queries return in well under a second. An
> *unconstrained* `?item ?prop ?date` forces QLever to scan all matching triples by a variable
> predicate — ~90s for a property with millions of statements, and heavy enough to risk
> destabilizing the server. Also keep `wikibase:limit` small here (`"1"` = best match), since
> each candidate property adds another predicate scan.

## Vocabulary

| Input `bd:serviceParam` | Meaning |
|---|---|
| `mwapi:search "<phrase>"` | search phrase (required) |
| `mwapi:type "property"` | must be `property` |
| `mwapi:language "en"` | informational (only English is indexed) |
| `wikibase:limit "N"` | max results, default 10, capped at 50 |

| Output triple | Binds |
|---|---|
| `?p wikibase:apiOutputItem mwapi:item` | entity URI `.../entity/Pxxx` (carries labels) |
| `?p wikibase:apiOutput mwapi:directProperty` | direct URI `.../prop/direct/Pxxx` (use as predicate) |
| `?l wikibase:apiOutput mwapi:label` | English label |
| `?s wikibase:apiOrdinal true` | 1-based rank |

## Config (env vars)

`QLEVER_URL` (default `http://localhost:7001`), `EMBED_URL`
(`http://localhost:8888/v1/embeddings`), `EMBED_MODEL` (`qwen3-embedding-8b`),
`PORT` (`7002`).
