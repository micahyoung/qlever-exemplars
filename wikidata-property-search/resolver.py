#!/usr/bin/env python3
"""Tier-3 fallback orchestration: control flow only, no HTTP, no Flask, no
module-level index loading. All external effects (LLM calls, live QLever
calls, persistence) are injected as callables, so this module is fully
unit-testable in isolation with fakes -- see test_resolver.py.

Two tiers already exist in server.py: exact lexical match, then embedding
similarity. Tier 3 fires only when both of those missed/scored too low, and
resolves via LLM-proposed permutations, verified deterministically rather
than trusted at face value:

  - property permutations are verified against the LOCAL lexical index
    (bounded ~13.5k properties -- if a permutation isn't real Wikidata
    vocabulary, it simply won't be a key in lex_index).
  - item/entity permutations are verified LIVE against QLever's rdfs:label
    (see qlever_client.py) -- this is what lets item resolution escape the
    fixed ~114k-entity ceiling of the precomputed class index.

Deliberately NOT done: re-embedding the original phrase and re-gating on the
same similarity threshold that already failed in tier 2. That was tried
during prototyping and rejected every correct answer -- it's circular, since
the phrase's poor embedding alignment is exactly why tier 2 failed in the
first place. Trust here is structural (exact match against real vocabulary),
not another semantic-similarity check.
"""
from build_index import embedding_text
from index_store import normalize


def should_attempt_tier3(phrase, bundle, top_score, min_score):
    """Tier-2->tier-3 routing decision, as a pure function so it's testable
    without a running server. Fires only when tier 1 ALSO missed (an exact
    lexical hit means tier 1 already succeeded, making tier 3 moot) AND
    tier 2's top embedding score is below `min_score`."""
    if normalize(phrase) in bundle.lex_index:
        return False
    return top_score < min_score


def first_lexical_hit(permutations, lex_index):
    """Return (matched_string, row_indices) for the FIRST permutation (in
    the LLM's own ranked order) that normalizes to a key in lex_index, else
    (None, None). Order-of-iteration decides the winner, not best embedding
    score among matches -- the LLM's own confidence ranking is the signal."""
    for p in permutations:
        rows = lex_index.get(normalize(p))
        if rows:
            return p, rows
    return None, None


def resolve_property_tier3(phrase, bundle, generate_permutations, persist):
    """
    generate_permutations: (phrase) -> list[str]
    persist: (row_index, alias) -> None
        Appends `alias` to the existing meta row at `row_index` (property
        tier-3 success attaches to an EXISTING row, never creates a new
        one). Persistence failures are swallowed here (logged, not raised)
        so a write error never discards an already-resolved hit -- but
        resolution failures (bad/empty LLM output) are NOT swallowed here;
        they propagate to the caller, which decides the tier-2 fallback.

    Returns the resolved meta dict, or None if tier 3 found nothing.
    """
    permutations = generate_permutations(phrase)
    if not permutations:
        return None

    matched, rows = first_lexical_hit(permutations, bundle.lex_index)
    if matched is None:
        return None

    row_index = rows[0]
    try:
        persist(row_index, phrase)
    except Exception as exc:  # noqa: BLE001 - best-effort cache write
        print(f"WARNING: tier-3 property persistence failed for {phrase!r}: {exc}", flush=True)

    return bundle.meta[row_index]


def resolve_item_tier3(
    phrase,
    generate_permutations,
    resolve_entity_uri,
    fetch_entity,
    embed,
    persist,
):
    """
    generate_permutations: (phrase) -> list[str]
    resolve_entity_uri: (name) -> str | None
        Live QLever verification + scoped notability disambiguation
        (qlever_client.resolve_entity_uri).
    fetch_entity: (uri) -> dict | None
        {"id", "uri", "label", "description", "aliases"} shaped like
        build_index.py's fetch_entities() rows.
    embed: (text) -> np.ndarray
        Embeds a plain text string (no instruction prefix -- matches
        build_index.py's document-embedding convention). resolve_item_tier3
        composes the text itself via build_index.embedding_text().
    persist: (entity_dict, vector) -> None
        Appends a brand-new row (genuinely new entity, never an existing
        one for the item case). Persistence failures are swallowed here for
        the same reason as resolve_property_tier3.

    Returns the resolved entity dict (with `phrase` folded into its
    aliases), or None if tier 3 found nothing across all permutations.
    """
    permutations = generate_permutations(phrase)
    if not permutations:
        return None

    uri = None
    for p in permutations:
        uri = resolve_entity_uri(p)
        if uri:
            break
    if uri is None:
        return None

    entity = fetch_entity(uri)
    if entity is None:
        return None

    existing_aliases = [a for a in entity.get("aliases", "").split(" | ") if a]
    if phrase not in existing_aliases:
        existing_aliases.append(phrase)
    entity = dict(entity, aliases=" | ".join(existing_aliases))

    vector = embed(embedding_text(entity))

    try:
        persist(entity, vector)
    except Exception as exc:  # noqa: BLE001 - best-effort cache write
        print(f"WARNING: tier-3 item persistence failed for {phrase!r}: {exc}", flush=True)

    return entity
