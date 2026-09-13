# Wikidata Property Semantic Search (SPARQL `SERVICE`)

A small, offline service that turns a **natural-language phrase into ranked Wikidata
property P-ids** by semantic similarity, callable inline from the local QLever endpoint
via SPARQL `SERVICE`. Its vocabulary mirrors query.wikidata.org's `wikibase:mwapi` so
federated queries feel native.

Unlike Wikidata's REST/`wbsearchentities` search (lexical, label/alias only — `"inception"`
returns just `P571`), this ranks by *meaning*, so `"inception"` also surfaces semantically
adjacent properties (`P580` start time, `P575` time of discovery, …).

**Ranking is three-tier:** an exact (case/whitespace-normalized) match on a property's label or
alias is pinned to the top; below that, everything else is ranked by pure embedding similarity;
and below a configurable similarity floor (`TIER3_MIN_SCORE`, default `0.70`), a local LLM is
asked to propose formal-name permutations of the phrase as a last resort. Tiers 1-2 keep the
semantic fan-out for conceptual phrases while guaranteeing that a phrase you *know* is a property
name (e.g. `"date of birth"` → `P569`) lands first rather than losing to a near-synonym whose
description happens to quote the phrase (`P3150` birthday). Tier 3 exists for phrases whose
wording is too indirect for the embedding model to align well at all (e.g. `"gig they do for a
living"` → `P106` occupation) — see **Tier 3 details** below.

## How it works

- `build_index.py` pulls all ~13.5k properties (label + description + aliases) from QLever
  (`:7001`), embeds a composed text per property via the local embedding server
  (`:8888`, `qwen3-embedding-8b`, 4096-dim), and saves `index/vectors.npy` + `index/meta.json`.
  It also builds `index_items/` the same way, from the ~114k entities that appear as the object
  of a `wdt:P31` ("instance of") triple somewhere in the dataset — a bounded "class/type"
  universe (`Q5` human, `Q515` city, …), not general entity search.
- `server.py` loads those vectors and serves a SPARQL-shaped endpoint on `:7002`. Per request
  it embeds the search phrase, does a brute-force cosine top-k (sub-10ms), and returns
  SPARQL-results JSON.
- `resolver.py`, `llm_client.py`, `qlever_client.py`, and `index_store.py` implement tier 3 (see
  below): LLM-proposed candidate permutations, deterministic verification, and persistence of
  successful resolutions so repeat queries become instant tier-1 hits.
- Tier-3 resolutions are **never** written into the files `build_index.py` produces. Each index
  has a small, separate "learned overlay" file (`index/learned_aliases.json`;
  `index_items/learned_entities.json` + `learned_vectors.npy`) that tier 3 appends to, merged with
  the base index in memory at server startup. This means the base files stay byte-for-byte what
  `build_index.py` produced — rebuildable/diffable/regenerable with total confidence — and
  "what has tier 3 added" is a direct read (`.venv/bin/python list_learned.py`) instead of a grep.

### Tier 3 details

Tier 3 only fires when tiers 1-2 both miss (no exact lexical match, and the best embedding score
is below `TIER3_MIN_SCORE`). It never trusts the LLM's answer directly — verification is
deterministic, and the two `mwapi:type`s are verified differently:

- **`"property"`**: an LLM-proposed permutation is checked against the same local lexical index
  tier 1 uses. If it isn't real Wikidata property vocabulary, it simply won't be a key in that
  index. On success, the original phrase is recorded as a new alias in `index/learned_aliases.json`
  (`{"pid", "alias", "added_at", "source_model"}`) and applied in-memory to the matched property —
  `index/meta.json` itself is never modified.
- **`"item"`**: since general entities were never candidates for the precomputed ~114k-entity
  class index in the first place, verification instead queries QLever **live** for an exact
  `rdfs:label` match. This is what lets item resolution reach entities outside that fixed
  universe (e.g. `"Marie Curie"`, `"the Big Apple"` → `Q60` New York City). A live label match
  can return many same-labeled candidates (dozens, in testing, for common city names); these are
  disambiguated by statement count *within that small candidate set only* — this is safe because
  the set is already filtered to one exact label, unlike a global notability ranking (which is
  dominated by bulk-imported, non-notable data on this dataset). On success, the resolved entity
  is embedded and appended as a new row + vector in `index_items/learned_entities.json` /
  `learned_vectors.npy` — `index_items/meta.json`/`vectors.npy` are never modified.

In both cases, a tier-3 miss (LLM timeout, malformed output, no permutation verifies) falls back
silently to the tier-2 result — the endpoint always returns `200`, never a tier-3-specific error.

**Auditing/undoing what tier 3 has added:** run `.venv/bin/python list_learned.py` to list every
learned alias/entity with its source model and timestamp. Because the learned files are small and
git-tracked separately from the (gitignored, regenerable) base files, undoing a bad resolution is
`git checkout -- index/learned_aliases.json` (or hand-editing/deleting the relevant entry) plus a
server restart — it can never corrupt or require rebuilding the base index.

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
| `mwapi:type "property"` \| `"item"` | `property` (default) resolves predicates; `item` resolves entities — the precomputed ~114k class/type universe via tiers 1-2, plus general named entities via the tier-3 live-QLever fallback (see Tier 3 details) |
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

**Tier 3:**

| Var | Default | Meaning |
|---|---|---|
| `TIER3_ENABLED` | `true` | master on/off switch |
| `TIER3_MIN_SCORE` | `0.70` | tier-2→tier-3 gate: fires when the best embedding score is below this |
| `TIER3_MAX_PERMUTATIONS` | `8` | cap on candidate permutations requested from / accepted from the LLM |
| `CHAT_URL` | `http://localhost:8888/v1/chat/completions` | chat-completions endpoint |
| `CHAT_MODEL` | `gemma-4-26b-a4b-vision` | model used for permutation generation — chosen after comparing several available models on real (non-hallucinated but sometimes semantically wrong) resolutions; this one was the only one that consistently avoided picking a real-but-wrong property when two plausible candidates existed, across repeated runs |
| `CHAT_TIMEOUT` | `120` (seconds) | per-call timeout; a timeout degrades to a tier-3 miss, not an error |
| `CHAT_ENABLE_THINKING` | `false` | maps to `chat_template_kwargs.enable_thinking` — **must** stay `false` for reasoning-capable models, or the model burns its whole token budget "thinking" and never returns an answer |
| `QLEVER_TIER3_TIMEOUT` | `20` (seconds) | timeout for the live QLever calls used only by item tier-3 (separate from `build_index.py`'s bulk-query timeouts) |

> **Why `CHAT_TIMEOUT` defaults so high:** if `EMBED_URL` and `CHAT_URL` point at the same
> single-GPU, swap-one-model-at-a-time gateway (as in local dev setups backed by e.g. llama.cpp's
> server or similar), every tier-3 request pays a *model-swap* cost, not just a generation cost —
> tier 2's `embed()` call runs first and loads the embedding model, evicting the chat model, so
> the following chat call almost always needs a full cold reload. Observed end-to-end latency for
> a single tier-3 chat call in that configuration ranged from under a second (model already warm)
> to 60-90+ seconds (cold swap under contention) during testing. A `CHAT_TIMEOUT` of `30` was
> tried first and caused frequent false-negative tier-3 misses (falling back to a worse tier-2
> result) purely from swap latency, not any actual resolution failure. If `EMBED_URL`/`CHAT_URL`
> point at *separate* backends (e.g. two GPUs, or a hosted API for one of them), this contention
> disappears and `CHAT_TIMEOUT` can safely be lowered.
