# Wikidata Property Semantic Search (SPARQL `SERVICE`)

A small, offline service that turns a **natural-language phrase into a Wikidata property
P-id (or item Q-id)**, callable inline from the local QLever endpoint via SPARQL `SERVICE`.
Its vocabulary mirrors query.wikidata.org's `wikibase:mwapi` so federated queries feel
native.

**Resolution is two-tier:** an exact (case/whitespace-normalized) match on a property's
label or alias wins outright — a phrase you *know* is a property name (e.g.
`"date of birth"` → `P569`) always resolves lexically, instantly. If no exact label/alias
match exists, a local LLM is asked to propose formal-name permutations of the phrase as a
fallback (tier 3) — e.g. `"gig they do for a living"` has no property literally labeled
that, but the LLM proposing `"occupation"` resolves to `P106` once that permutation is
checked against the same lexical index. Tier 3 is deliberately last-resort: it's
LLM-round-trip latency on a cache miss, versus sub-millisecond for tier 1.

*(An earlier version of this service ranked non-exact matches by embedding similarity as
a middle tier. That tier was removed — see `index_store.py`'s and `resolver.py`'s comments
for what it used to do — so a phrase that doesn't exactly match a label/alias now either
gets picked up by the LLM fallback or fails to resolve; there is no more fuzzy nearest-
neighbor ranking.)*

## How it works

- `build_index.py` pulls all ~13.5k properties (label + description + aliases) from QLever
  (`:7001`) and saves `index/meta.json`. It also builds `index_items/meta.json` the same
  way, from the ~114k entities that appear as the object of a `wdt:P31` ("instance of")
  triple somewhere in the dataset — a bounded "class/type" universe (`Q5` human, `Q515`
  city, …), not general entity search.
- `server.py` loads those files into an in-memory lexical index (label/alias →
  candidate rows, case/whitespace-normalized) and serves a SPARQL-shaped endpoint on
  `:7002`. A request either hits that index directly (tier 1) or falls through to tier 3.
- `resolver.py`, `llm_client.py`, `qlever_client.py`, and `index_store.py` implement tier 3:
  LLM-proposed candidate permutations, deterministic verification, and persistence of
  successful resolutions so repeat queries become instant tier-1 hits.
- Tier-3 resolutions are **never** written into the files `build_index.py` produces. Each
  index has a small, separate "learned overlay" file (`index/learned_aliases.json`;
  `index_items/learned_entities.json`) that tier 3 appends to, merged with the base index
  in memory at server startup. This means the base files stay byte-for-byte what
  `build_index.py` produced — rebuildable/diffable/regenerable with total confidence — and
  "what has tier 3 added" is a direct read (`.venv/bin/python list_learned.py`) instead of
  a grep.

### Tier 3 details

Tier 3 only fires when tier 1 misses (no exact lexical label/alias match at all). It never
trusts the LLM's answer directly — verification is deterministic, and the two `mwapi:type`s
are verified differently:

- **`"property"`**: an LLM-proposed permutation is checked against the same local lexical
  index tier 1 uses. If it isn't real Wikidata property vocabulary, it simply won't be a key
  in that index. On success, the original phrase is recorded as a new alias in
  `index/learned_aliases.json` (`{"pid", "alias", "added_at", "source_model"}`) and applied
  in-memory to the matched property — `index/meta.json` itself is never modified.
- **`"item"`**: since general entities were never candidates for the precomputed ~114k-entity
  class index in the first place, verification instead queries QLever **live** for an exact
  `rdfs:label` match. This is what lets item resolution reach entities outside that fixed
  universe (e.g. `"Marie Curie"`, `"the Big Apple"` → `Q60` New York City). A live label match
  can return many same-labeled candidates (dozens, in testing, for common city names); these
  are disambiguated by statement count *within that small candidate set only* — this is safe
  because the set is already filtered to one exact label, unlike a global notability ranking
  (which is dominated by bulk-imported, non-notable data on this dataset). On success, the
  resolved entity is appended as a new row in `index_items/learned_entities.json` —
  `index_items/meta.json` is never modified.

A tier-3 **miss** (LLM timeout, malformed output, no permutation verifies) raises and the
endpoint returns a non-2xx response — it does not fall back to an empty/silent `200`. See
CLAUDE.md for why: QLever's own result cache stores any HTTP-200 SERVICE response
indefinitely, even an empty-bindings one, so a transient failure returned as "success" would
get stuck cached as a false permanent non-match.

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

# QLever (:7001) must be running to build the index; the chat/LLM backend
# (:8888 by default, see CHAT_URL below) must be running to serve tier-3 fallbacks.
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
| `mwapi:type "property"` \| `"item"` | `property` (default) resolves predicates; `item` resolves entities — the precomputed ~114k class/type universe via tier 1, plus general named entities via the tier-3 live-QLever fallback (see Tier 3 details) |
| `mwapi:language "en"` | informational (only English is indexed) |
| `wikibase:limit "N"` | max results, default 10, capped at 50 |

| Output triple | Binds |
|---|---|
| `?p wikibase:apiOutputItem mwapi:item` | entity URI `.../entity/Pxxx` (carries labels) |
| `?p wikibase:apiOutput mwapi:directProperty` | direct URI `.../prop/direct/Pxxx` (use as predicate) |
| `?l wikibase:apiOutput mwapi:label` | English label |
| `?s wikibase:apiOrdinal true` | 1-based rank |

## Config (env vars)

`QLEVER_URL` (default `http://localhost:7001`), `PORT` (`7002`).

**Tier 3:**

| Var | Default | Meaning |
|---|---|---|
| `TIER3_MAX_PERMUTATIONS` | `8` | cap on candidate permutations requested from / accepted from the LLM |
| `CHAT_URL` | `http://localhost:8888/v1/chat/completions` | chat-completions endpoint |
| `CHAT_MODEL` | `gemma-4-26b-a4b-vision` | model used for permutation generation — chosen after comparing several available models on real (non-hallucinated but sometimes semantically wrong) resolutions; this one was the only one that consistently avoided picking a real-but-wrong property when two plausible candidates existed, across repeated runs |
| `CHAT_TIMEOUT` | `120` (seconds) | per-call timeout; a timeout degrades to a tier-3 miss, not an error |
| `CHAT_ENABLE_THINKING` | `false` | maps to `chat_template_kwargs.enable_thinking` — **must** stay `false` for reasoning-capable models, or the model burns its whole token budget "thinking" and never returns an answer |
| `QLEVER_TIER3_TIMEOUT` | `20` (seconds) | timeout for the live QLever calls used only by item tier-3 (separate from `build_index.py`'s bulk-query timeouts) |

> **Why `CHAT_TIMEOUT` defaults so high:** if `CHAT_URL` points at a shared, swap-one-model-
> at-a-time inference backend also used by other services (as in local dev setups backed by
> e.g. llama.cpp's server or similar), a tier-3 request can pay a *model-swap* cost, not just
> a generation cost, whenever something else last used that backend for a different model.
> Observed end-to-end latency for a single tier-3 chat call in that configuration ranged from
> under a second (model already warm) to 60-90+ seconds (cold swap under contention) during
> testing. A `CHAT_TIMEOUT` of `30` was tried first and caused frequent false-negative tier-3
> misses purely from swap latency, not any actual resolution failure. If `CHAT_URL` points at
> a dedicated backend with no contention, this can safely be lowered.
