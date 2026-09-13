#!/usr/bin/env python3
"""Tier-3 fallback orchestration: control flow only, no HTTP, no Flask, no
module-level index loading. All external effects (LLM calls, live QLever
calls, persistence) are injected as callables, so this module is fully
unit-testable in isolation with fakes -- see test_resolver.py.

One tier already exists in server.py: exact lexical match. Tier 3 fires
whenever that missed, and resolves via LLM-proposed candidates, verified
deterministically rather than trusted at face value:

  - property permutations are verified against the LOCAL lexical index
    (bounded ~13.5k properties -- if a permutation isn't real Wikidata
    vocabulary, it simply won't be a key in lex_index).
  - item/entity permutations are verified LIVE against QLever's rdfs:label
    (see qlever_client.py) -- this is what lets item resolution escape the
    fixed ~114k-entity ceiling of the precomputed class index.
  - relation pairs (resolve_relation_tier3) verify BOTH halves individually
    as above, AND live-verify the pair as a whole (qlever_client.triple_exists)
    -- a plausible-but-wrong property can individually exist and still be
    the wrong choice paired with a given item, so the pair itself is checked.
"""
from index_store import normalize

# Property meta rows store the ENTITY-form uri (.../entity/Pxxx); predicates
# in actual triples use the DIRECT-property form (.../prop/direct/Pxxx).
ENTITY_PREFIX = "http://www.wikidata.org/entity/"
DIRECT_PREFIX = "http://www.wikidata.org/prop/direct/"


def should_attempt_tier3(phrase, bundle):
    """Routing decision, as a pure function so it's testable without a
    running server. Fires whenever tier 1 missed (no exact lexical hit)."""
    return normalize(phrase) not in bundle.lex_index


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
    persist: (entity_dict) -> None
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

    try:
        persist(entity)
    except Exception as exc:  # noqa: BLE001 - best-effort cache write
        print(f"WARNING: tier-3 item persistence failed for {phrase!r}: {exc}", flush=True)

    return entity


def resolve_relation_tier3(
    phrase,
    generate_relation_pairs,
    property_bundle,
    item_bundle,
    resolve_entity_uri,
    fetch_entity,
    triple_exists,
    persist_property_alias,
    persist_item_row,
    persist_relation_cache,
):
    """
    Jointly resolves a "?s prop item" relation phrase (e.g. "received the
    Nobel Prize in Physics") into a verified (property_meta, item_meta)
    pair, giving the LLM real cross-phrase context instead of resolving the
    property and item independently and blindly recombining them.

    generate_relation_pairs: (phrase) -> list[(property_label, item_label)]
    resolve_entity_uri, fetch_entity: same as resolve_item_tier3's args.
    triple_exists: (prop_uri, item_uri) -> bool
        Live check that `?s <prop_uri> <item_uri>` actually occurs in the
        dataset -- a plausible-but-wrong property can individually exist
        (and even individually verify against the lexical index) while
        still being the wrong choice paired with this specific item.
    persist_property_alias: (row_index, alias) -> None
    persist_item_row: (entity_dict) -> None
    persist_relation_cache: (phrase, pid, qid) -> None
        Thin phrase -> (pid, qid) cache entry, distinct from the property
        alias / item row persists above: the free-text relation phrase
        itself ("received the Nobel Prize in Physics") won't exact-match
        either lexical index, so a repeat identical phrase needs its own
        cache to skip straight back to this exact pair instead of
        re-running tier 3.

    Tries each LLM-proposed pair IN ORDER (most confident first); the FIRST
    pair whose property verifies against the local lexical index, whose
    item verifies live against QLever, AND whose triple co-occurs in the
    dataset wins. Persists all three facts (each independently, each
    persistence failure swallowed/logged rather than discarding the hit)
    and returns (property_meta, item_meta), or (None, None) if every
    proposed pair fails.
    """
    pairs = generate_relation_pairs(phrase)
    if not pairs:
        return None, None

    for prop_label, item_label in pairs:
        prop_matched, prop_rows = first_lexical_hit([prop_label], property_bundle.lex_index)
        if prop_matched is None:
            continue
        prop_row = prop_rows[0]
        prop_meta = property_bundle.meta[prop_row]

        item_uri = resolve_entity_uri(item_label)
        if item_uri is None:
            continue
        item_meta = fetch_entity(item_uri)
        if item_meta is None:
            continue

        direct_prop_uri = prop_meta["uri"].replace(ENTITY_PREFIX, DIRECT_PREFIX)
        if not triple_exists(direct_prop_uri, item_meta["uri"]):
            continue

        try:
            persist_property_alias(prop_row, prop_label)
        except Exception as exc:  # noqa: BLE001 - best-effort cache write
            print(f"WARNING: relation tier-3 property persistence failed for {phrase!r}: {exc}", flush=True)
        try:
            persist_item_row(item_meta)
        except Exception as exc:  # noqa: BLE001 - best-effort cache write
            print(f"WARNING: relation tier-3 item persistence failed for {phrase!r}: {exc}", flush=True)
        try:
            persist_relation_cache(phrase, prop_meta["pid"], item_meta["id"])
        except Exception as exc:  # noqa: BLE001 - best-effort cache write
            print(f"WARNING: relation cache persistence failed for {phrase!r}: {exc}", flush=True)

        return prop_meta, item_meta

    return None, None
