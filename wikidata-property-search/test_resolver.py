"""Control-flow tests for resolver.py's tier-3 orchestration. All external
effects (LLM calls, live QLever verification, persistence) are injected as
plain fake callables -- no HTTP, no file I/O, no real IndexBundle files.
resolver.py never imports server.py, so importing it here never triggers
real index loading."""
import pytest

from index_store import IndexBundle
import resolver


def make_bundle(meta, lex_index=None):
    """Small in-memory fake IndexBundle. `vectors`/`instruction` are unused
    by resolver.py's control flow, so they're left as placeholders."""
    if lex_index is None:
        lex_index = {}
    return IndexBundle(vectors=None, meta=meta, lex_index=lex_index, instruction="")


# --- should_attempt_tier3 (threshold gating) -------------------------------

def test_not_invoked_when_lex_hit_exists():
    bundle = make_bundle(meta=[{"label": "occupation"}], lex_index={"occupation": [0]})
    assert resolver.should_attempt_tier3("occupation", bundle, top_score=0.1, min_score=0.7) is False


def test_not_invoked_when_top_score_above_threshold():
    bundle = make_bundle(meta=[], lex_index={})
    assert resolver.should_attempt_tier3("phrase", bundle, top_score=0.85, min_score=0.70) is False


def test_invoked_when_no_lex_hit_and_score_below_threshold():
    bundle = make_bundle(meta=[], lex_index={})
    assert resolver.should_attempt_tier3("phrase", bundle, top_score=0.55, min_score=0.70) is True


def test_boundary_score_equal_to_threshold_not_invoked():
    bundle = make_bundle(meta=[], lex_index={})
    assert resolver.should_attempt_tier3("phrase", bundle, top_score=0.70, min_score=0.70) is False


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
    # must win even though nothing about embedding score is consulted here.
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
    tier-2 fallback."""
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
        embed=lambda text: "fake-vector",
        persist=lambda e, v: persisted.append((e, v)),
    )

    assert resolve_calls == ["Apple Inc."]  # stopped after first success
    assert result["uri"] == "http://www.wikidata.org/entity/Q312"
    assert "the fruit company Steve Jobs started" in result["aliases"]
    assert persisted == [(result, "fake-vector")]


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
        embed=lambda text: "vec",
        persist=lambda e, v: None,
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
        embed=lambda text: "vec",
        persist=lambda e, v: persisted.append((e, v)),
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
        embed=lambda text: "vec",
        persist=lambda e, v: None,
    )

    assert result is None
    assert calls == []


def test_item_tier3_fetch_entity_none_returns_none():
    result = resolver.resolve_item_tier3(
        "phrase",
        generate_permutations=lambda phrase: ["Some Name"],
        resolve_entity_uri=lambda name: "Q999",
        fetch_entity=lambda uri: None,
        embed=lambda text: "vec",
        persist=lambda e, v: None,
    )
    assert result is None


def test_item_tier3_persist_failure_does_not_discard_resolved_hit():
    entity = {"id": "Q60", "uri": "Q60", "label": "New York City", "description": "", "aliases": ""}

    def failing_persist(e, v):
        raise OSError("disk full")

    result = resolver.resolve_item_tier3(
        "the Big Apple",
        generate_permutations=lambda phrase: ["New York City"],
        resolve_entity_uri=lambda name: "Q60",
        fetch_entity=lambda uri: entity,
        embed=lambda text: "vec",
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
            embed=lambda text: "vec",
            persist=lambda e, v: None,
        )
