"""Control-flow tests for resolver.py's tier-3 orchestration. All external
effects (LLM calls, live QLever verification, persistence) are injected as
plain fake callables -- no HTTP, no file I/O, no real IndexBundle files.
resolver.py never imports server.py, so importing it here never triggers
real index loading."""
import pytest

from index_store import IndexBundle
import resolver


def make_bundle(meta, lex_index=None):
    if lex_index is None:
        lex_index = {}
    return IndexBundle(meta=meta, lex_index=lex_index)


# --- should_attempt_tier3 ---------------------------------------------------

def test_not_invoked_when_lex_hit_exists():
    bundle = make_bundle(meta=[{"label": "occupation"}], lex_index={"occupation": [0]})
    assert resolver.should_attempt_tier3("occupation", bundle) is False


def test_invoked_when_no_lex_hit():
    bundle = make_bundle(meta=[], lex_index={})
    assert resolver.should_attempt_tier3("phrase", bundle) is True


# --- first_lexical_hit -------------------------------------------------------

def test_first_lexical_hit_picks_first_matching_permutation_in_order():
    lex_index = {"field of work": [4], "occupation": [2]}
    permutations = ["employer", "industry", "occupation", "position held", "field of work"]
    matched, rows = resolver.first_lexical_hit(permutations, lex_index)
    assert matched == "occupation"
    assert rows == [2]


def test_first_lexical_hit_no_match_returns_none_none():
    matched, rows = resolver.first_lexical_hit(["nope", "nada"], {"occupation": [2]})
    assert matched is None and rows is None


def test_first_lexical_hit_empty_permutations_returns_none_none():
    matched, rows = resolver.first_lexical_hit([], {"occupation": [2]})
    assert matched is None and rows is None


# --- resolve_property_tier3 --------------------------------------------------

def test_property_tier3_picks_first_matching_permutation_not_best_score():
    # Both "field of work" (index 3) and "occupation" (index 1) exist in the
    # lex index; "occupation" comes first in the LLM's ranked order, so it
    # must win regardless of any other ordering signal.
    meta = [
        {"label": "employer", "pid": "P108", "uri": "u108"},
        {"label": "occupation", "pid": "P106", "uri": "u106"},
        {"label": "industry", "pid": "P452", "uri": "u452"},
        {"label": "field of work", "pid": "P101", "uri": "u101"},
    ]
    lex_index = {"occupation": [1], "field of work": [3]}
    bundle = make_bundle(meta, lex_index)

    permutations = ["occupation", "employer", "industry", "field of work"]
    persisted = []

    result = resolver.resolve_property_tier3(
        "gig they do for a living",
        bundle,
        generate_permutations=lambda phrase: permutations,
        persist=lambda row_index, alias: persisted.append((row_index, alias)),
    )

    assert result == meta[1]
    assert persisted == [(1, "gig they do for a living")]


def test_property_tier3_no_permutation_matches_returns_none():
    bundle = make_bundle(meta=[{"label": "occupation"}], lex_index={"occupation": [0]})
    persisted = []

    result = resolver.resolve_property_tier3(
        "totally unrelated phrase",
        bundle,
        generate_permutations=lambda phrase: ["nonsense", "gibberish"],
        persist=lambda row_index, alias: persisted.append((row_index, alias)),
    )

    assert result is None
    assert persisted == []


def test_property_tier3_empty_permutations_short_circuits_without_lookup():
    bundle = make_bundle(meta=[{"label": "occupation"}], lex_index={"occupation": [0]})
    persisted = []

    result = resolver.resolve_property_tier3(
        "phrase",
        bundle,
        generate_permutations=lambda phrase: [],
        persist=lambda row_index, alias: persisted.append((row_index, alias)),
    )

    assert result is None
    assert persisted == []


def test_property_tier3_resolution_failure_propagates():
    """generate_permutations raising (e.g. a chat-model timeout) is NOT
    swallowed inside resolver.py -- the caller (server.py) decides the
    clean-miss fallback."""
    bundle = make_bundle(meta=[], lex_index={})

    def raises(phrase):
        raise TimeoutError("chat model timed out")

    with pytest.raises(TimeoutError):
        resolver.resolve_property_tier3(
            "phrase", bundle, generate_permutations=raises, persist=lambda *a: None
        )


def test_property_tier3_persist_failure_does_not_discard_resolved_hit():
    meta = [{"label": "occupation", "pid": "P106", "uri": "u106"}]
    bundle = make_bundle(meta, lex_index={"occupation": [0]})

    def failing_persist(row_index, alias):
        raise OSError("disk full")

    result = resolver.resolve_property_tier3(
        "gig they do for a living",
        bundle,
        generate_permutations=lambda phrase: ["occupation"],
        persist=failing_persist,
    )

    assert result == meta[0]


# --- resolve_item_tier3 -------------------------------------------------------

def test_item_tier3_first_permutation_resolves_no_further_calls():
    resolve_calls = []

    def fake_resolve_entity_uri(name):
        resolve_calls.append(name)
        return "http://www.wikidata.org/entity/Q312"

    entity = {"id": "Q312", "uri": "http://www.wikidata.org/entity/Q312",
              "label": "Apple Inc.", "description": "tech company", "aliases": ""}
    persisted = []

    result = resolver.resolve_item_tier3(
        "the fruit company Steve Jobs started",
        generate_permutations=lambda phrase: ["Apple Inc.", "Apple Computer"],
        resolve_entity_uri=fake_resolve_entity_uri,
        fetch_entity=lambda uri: entity,
        persist=lambda e: persisted.append(e),
    )

    assert resolve_calls == ["Apple Inc."]  # stopped after first success
    assert result["uri"] == "http://www.wikidata.org/entity/Q312"
    assert "the fruit company Steve Jobs started" in result["aliases"]
    assert persisted == [result]


def test_item_tier3_iterates_past_permutations_with_zero_candidates():
    # permutation[0] resolves to nothing live; permutation[1] resolves.
    resolve_map = {"Chicago Illinois USA": None, "Chicago": "Q1297"}
    resolve_calls = []

    def fake_resolve_entity_uri(name):
        resolve_calls.append(name)
        return resolve_map[name]

    entity = {"id": "Q1297", "uri": "Q1297", "label": "Chicago", "description": "", "aliases": ""}

    result = resolver.resolve_item_tier3(
        "the Windy City",
        generate_permutations=lambda phrase: ["Chicago Illinois USA", "Chicago"],
        resolve_entity_uri=fake_resolve_entity_uri,
        fetch_entity=lambda uri: entity,
        persist=lambda e: None,
    )

    assert resolve_calls == ["Chicago Illinois USA", "Chicago"]
    assert result["uri"] == "Q1297"


def test_item_tier3_all_permutations_fail_returns_none():
    persisted = []

    result = resolver.resolve_item_tier3(
        "completely made up nonsense",
        generate_permutations=lambda phrase: ["nonsense one", "nonsense two"],
        resolve_entity_uri=lambda name: None,
        fetch_entity=lambda uri: {"label": "should not be called"},
        persist=lambda e: persisted.append(e),
    )

    assert result is None
    assert persisted == []


def test_item_tier3_empty_permutations_short_circuits():
    calls = []

    result = resolver.resolve_item_tier3(
        "phrase",
        generate_permutations=lambda phrase: [],
        resolve_entity_uri=lambda name: calls.append(name) or "should-not-happen",
        fetch_entity=lambda uri: None,
        persist=lambda e: None,
    )

    assert result is None
    assert calls == []


def test_item_tier3_fetch_entity_none_returns_none():
    result = resolver.resolve_item_tier3(
        "phrase",
        generate_permutations=lambda phrase: ["Some Name"],
        resolve_entity_uri=lambda name: "Q999",
        fetch_entity=lambda uri: None,
        persist=lambda e: None,
    )
    assert result is None


def test_item_tier3_persist_failure_does_not_discard_resolved_hit():
    entity = {"id": "Q60", "uri": "Q60", "label": "New York City", "description": "", "aliases": ""}

    def failing_persist(e):
        raise OSError("disk full")

    result = resolver.resolve_item_tier3(
        "the Big Apple",
        generate_permutations=lambda phrase: ["New York City"],
        resolve_entity_uri=lambda name: "Q60",
        fetch_entity=lambda uri: entity,
        persist=failing_persist,
    )

    assert result is not None
    assert result["uri"] == "Q60"


def test_item_tier3_resolution_failure_propagates():
    def raises(phrase):
        raise TimeoutError("qlever timed out")

    with pytest.raises(TimeoutError):
        resolver.resolve_item_tier3(
            "phrase",
            generate_permutations=lambda phrase: ["Something"],
            resolve_entity_uri=raises,
            fetch_entity=lambda uri: None,
            persist=lambda e: None,
        )


# --- resolve_relation_tier3 ---------------------------------------------------

def _property_bundle():
    meta = [
        {"label": "award received", "pid": "P166", "uri": "http://www.wikidata.org/entity/P166"},
        {"label": "nominated for", "pid": "P1411", "uri": "http://www.wikidata.org/entity/P1411"},
    ]
    lex_index = {"award received": [0], "nominated for": [1]}
    return make_bundle(meta, lex_index)


def _item_entity(qid="Q38104", label="Nobel Prize in Physics"):
    return {"id": qid, "uri": f"http://www.wikidata.org/entity/{qid}", "label": label,
            "description": "", "aliases": ""}


def test_relation_tier3_first_pair_passes_all_checks():
    property_bundle = _property_bundle()
    item = _item_entity()
    persisted_props, persisted_items, persisted_cache = [], [], []

    prop_meta, item_meta = resolver.resolve_relation_tier3(
        "received the Nobel Prize in Physics",
        generate_relation_pairs=lambda phrase: [("award received", "Nobel Prize in Physics")],
        property_bundle=property_bundle,
        item_bundle=None,
        resolve_entity_uri=lambda name: item["uri"],
        fetch_entity=lambda uri: item,
        triple_exists=lambda prop_uri, item_uri: True,
        persist_property_alias=lambda row_index, alias: persisted_props.append((row_index, alias)),
        persist_item_row=lambda entity: persisted_items.append(entity),
        persist_relation_cache=lambda phrase, pid, qid: persisted_cache.append((phrase, pid, qid)),
    )

    assert prop_meta["pid"] == "P166"
    assert item_meta["id"] == "Q38104"
    assert persisted_props == [(0, "award received")]
    assert persisted_items == [item]
    assert persisted_cache == [("received the Nobel Prize in Physics", "P166", "Q38104")]


def test_relation_tier3_property_check_fails_falls_through_to_next_pair():
    property_bundle = _property_bundle()
    item = _item_entity()

    prop_meta, item_meta = resolver.resolve_relation_tier3(
        "won the Nobel Prize in Physics",
        generate_relation_pairs=lambda phrase: [
            ("not a real property", "Nobel Prize in Physics"),
            ("award received", "Nobel Prize in Physics"),
        ],
        property_bundle=property_bundle,
        item_bundle=None,
        resolve_entity_uri=lambda name: item["uri"],
        fetch_entity=lambda uri: item,
        triple_exists=lambda prop_uri, item_uri: True,
        persist_property_alias=lambda *a: None,
        persist_item_row=lambda *a: None,
        persist_relation_cache=lambda *a: None,
    )

    assert prop_meta["pid"] == "P166"
    assert item_meta["id"] == "Q38104"


def test_relation_tier3_item_check_fails_falls_through_to_next_pair():
    property_bundle = _property_bundle()
    item = _item_entity()

    def fake_resolve_entity_uri(name):
        return None if name == "not a real item" else item["uri"]

    prop_meta, item_meta = resolver.resolve_relation_tier3(
        "won the Nobel Prize in Physics",
        generate_relation_pairs=lambda phrase: [
            ("award received", "not a real item"),
            ("award received", "Nobel Prize in Physics"),
        ],
        property_bundle=property_bundle,
        item_bundle=None,
        resolve_entity_uri=fake_resolve_entity_uri,
        fetch_entity=lambda uri: item,
        triple_exists=lambda prop_uri, item_uri: True,
        persist_property_alias=lambda *a: None,
        persist_item_row=lambda *a: None,
        persist_relation_cache=lambda *a: None,
    )

    assert prop_meta["pid"] == "P166"
    assert item_meta["id"] == "Q38104"


def test_relation_tier3_triple_exists_fails_falls_through_to_next_pair():
    """The key new correctness case: "nominated for" individually exists as
    a real property and "Nobel Prize in Physics" individually exists as a
    real item, but the pairing itself is wrong -- triple_exists rejects it,
    and the next candidate pair ("award received") is tried instead."""
    property_bundle = _property_bundle()
    item = _item_entity()

    def fake_triple_exists(prop_uri, item_uri):
        return "P1411" not in prop_uri  # "nominated for" pairing is wrong; "award received" is right

    prop_meta, item_meta = resolver.resolve_relation_tier3(
        "won the Nobel Prize in Physics",
        generate_relation_pairs=lambda phrase: [
            ("nominated for", "Nobel Prize in Physics"),
            ("award received", "Nobel Prize in Physics"),
        ],
        property_bundle=property_bundle,
        item_bundle=None,
        resolve_entity_uri=lambda name: item["uri"],
        fetch_entity=lambda uri: item,
        triple_exists=fake_triple_exists,
        persist_property_alias=lambda *a: None,
        persist_item_row=lambda *a: None,
        persist_relation_cache=lambda *a: None,
    )

    assert prop_meta["pid"] == "P166"
    assert item_meta["id"] == "Q38104"


def test_relation_tier3_all_pairs_exhausted_returns_none_none():
    property_bundle = _property_bundle()

    prop_meta, item_meta = resolver.resolve_relation_tier3(
        "complete nonsense relation",
        generate_relation_pairs=lambda phrase: [("not real", "also not real")],
        property_bundle=property_bundle,
        item_bundle=None,
        resolve_entity_uri=lambda name: None,
        fetch_entity=lambda uri: None,
        triple_exists=lambda prop_uri, item_uri: True,
        persist_property_alias=lambda *a: None,
        persist_item_row=lambda *a: None,
        persist_relation_cache=lambda *a: None,
    )

    assert prop_meta is None
    assert item_meta is None


def test_relation_tier3_empty_pairs_short_circuits():
    property_bundle = _property_bundle()

    def should_not_be_called(name):
        raise AssertionError("should not be called")

    prop_meta, item_meta = resolver.resolve_relation_tier3(
        "phrase",
        generate_relation_pairs=lambda phrase: [],
        property_bundle=property_bundle,
        item_bundle=None,
        resolve_entity_uri=should_not_be_called,
        fetch_entity=lambda uri: None,
        triple_exists=lambda *a: True,
        persist_property_alias=lambda *a: None,
        persist_item_row=lambda *a: None,
        persist_relation_cache=lambda *a: None,
    )

    assert prop_meta is None
    assert item_meta is None


def test_relation_tier3_property_persist_failure_does_not_discard_hit():
    property_bundle = _property_bundle()
    item = _item_entity()

    def failing_persist_property(row_index, alias):
        raise OSError("disk full")

    prop_meta, item_meta = resolver.resolve_relation_tier3(
        "received the Nobel Prize in Physics",
        generate_relation_pairs=lambda phrase: [("award received", "Nobel Prize in Physics")],
        property_bundle=property_bundle,
        item_bundle=None,
        resolve_entity_uri=lambda name: item["uri"],
        fetch_entity=lambda uri: item,
        triple_exists=lambda *a: True,
        persist_property_alias=failing_persist_property,
        persist_item_row=lambda *a: None,
        persist_relation_cache=lambda *a: None,
    )

    assert prop_meta["pid"] == "P166"
    assert item_meta["id"] == "Q38104"


def test_relation_tier3_item_persist_failure_does_not_discard_hit():
    property_bundle = _property_bundle()
    item = _item_entity()

    def failing_persist_item(entity):
        raise OSError("disk full")

    prop_meta, item_meta = resolver.resolve_relation_tier3(
        "received the Nobel Prize in Physics",
        generate_relation_pairs=lambda phrase: [("award received", "Nobel Prize in Physics")],
        property_bundle=property_bundle,
        item_bundle=None,
        resolve_entity_uri=lambda name: item["uri"],
        fetch_entity=lambda uri: item,
        triple_exists=lambda *a: True,
        persist_property_alias=lambda *a: None,
        persist_item_row=failing_persist_item,
        persist_relation_cache=lambda *a: None,
    )

    assert prop_meta["pid"] == "P166"
    assert item_meta["id"] == "Q38104"


def test_relation_tier3_cache_persist_failure_does_not_discard_hit():
    property_bundle = _property_bundle()
    item = _item_entity()

    def failing_persist_cache(phrase, pid, qid):
        raise OSError("disk full")

    prop_meta, item_meta = resolver.resolve_relation_tier3(
        "received the Nobel Prize in Physics",
        generate_relation_pairs=lambda phrase: [("award received", "Nobel Prize in Physics")],
        property_bundle=property_bundle,
        item_bundle=None,
        resolve_entity_uri=lambda name: item["uri"],
        fetch_entity=lambda uri: item,
        triple_exists=lambda *a: True,
        persist_property_alias=lambda *a: None,
        persist_item_row=lambda *a: None,
        persist_relation_cache=failing_persist_cache,
    )

    assert prop_meta["pid"] == "P166"
    assert item_meta["id"] == "Q38104"


def test_relation_tier3_resolution_failure_propagates():
    property_bundle = _property_bundle()

    def raises(phrase):
        raise TimeoutError("chat model timed out")

    with pytest.raises(TimeoutError):
        resolver.resolve_relation_tier3(
            "phrase",
            generate_relation_pairs=raises,
            property_bundle=property_bundle,
            item_bundle=None,
            resolve_entity_uri=lambda name: None,
            fetch_entity=lambda uri: None,
            triple_exists=lambda *a: True,
            persist_property_alias=lambda *a: None,
            persist_item_row=lambda *a: None,
            persist_relation_cache=lambda *a: None,
        )
