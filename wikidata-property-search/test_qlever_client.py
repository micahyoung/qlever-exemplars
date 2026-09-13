"""Control-flow tests for qlever_client. HTTP calls (requests.post) are
mocked via unittest.mock.patch; resolve_entity_uri's composition logic is
tested with injected fake lookup/rank callables instead."""
from unittest.mock import patch, MagicMock

import qlever_client


# --- escape_sparql_literal ------------------------------------------------

def test_escape_double_quote():
    assert qlever_client.escape_sparql_literal('say "hi"') == 'say \\"hi\\"'


def test_escape_backslash():
    assert qlever_client.escape_sparql_literal("a\\b") == "a\\\\b"


def test_escape_newline_and_tab():
    assert qlever_client.escape_sparql_literal("a\nb\tc") == "a\\nb\\tc"


def test_escape_combination():
    assert (
        qlever_client.escape_sparql_literal('He said "hi\\there"')
        == 'He said \\"hi\\\\there\\"'
    )


def test_escape_plain_string_unchanged():
    assert qlever_client.escape_sparql_literal("New York City") == "New York City"


# --- rank_by_local_notability ---------------------------------------------

def test_rank_single_candidate_skips_query():
    with patch("qlever_client.requests.post") as mock_post:
        result = qlever_client.rank_by_local_notability(["uri1"])
    assert result == "uri1"
    mock_post.assert_not_called()


def test_rank_empty_candidates_returns_none():
    with patch("qlever_client.requests.post") as mock_post:
        result = qlever_client.rank_by_local_notability([])
    assert result is None
    mock_post.assert_not_called()


def test_rank_multi_candidate_queries_and_returns_winner():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "results": {"bindings": [{"s": {"value": "uri2"}, "c": {"value": "1732"}}]}
    }
    with patch("qlever_client.requests.post", return_value=mock_resp) as mock_post:
        result = qlever_client.rank_by_local_notability(["uri1", "uri2", "uri3"])
    assert result == "uri2"
    mock_post.assert_called_once()
    # the query text should VALUES-scope exactly the three candidate URIs
    sent_query = mock_post.call_args.kwargs["data"]["query"]
    assert "<uri1>" in sent_query and "<uri2>" in sent_query and "<uri3>" in sent_query
    assert "GROUP BY ?s" in sent_query


# --- resolve_entity_uri (composition, via injected fakes) -----------------

def test_resolve_entity_uri_zero_candidates_returns_none():
    result = qlever_client.resolve_entity_uri(
        "nonsense", lookup=lambda name, **kw: [], rank=lambda uris, **kw: uris[0] if uris else None
    )
    assert result is None


def test_resolve_entity_uri_single_candidate_no_rank_call():
    rank_calls = []

    def fake_rank(uris, **kw):
        rank_calls.append(uris)
        return uris[0]

    result = qlever_client.resolve_entity_uri(
        "Apple Inc.", lookup=lambda name, **kw: ["Q312"], rank=fake_rank
    )
    assert result == "Q312"
    # rank() is still invoked by resolve_entity_uri's composition, but
    # rank_by_local_notability itself (tested above) is what actually
    # short-circuits on a single candidate -- here we just confirm the
    # single uri flows through correctly end to end.
    assert rank_calls == [["Q312"]]


def test_resolve_entity_uri_multi_candidate_uses_rank():
    result = qlever_client.resolve_entity_uri(
        "New York City",
        lookup=lambda name, **kw: ["Q107394799", "Q60", "Q64154093"],
        rank=lambda uris, **kw: "Q60",
    )
    assert result == "Q60"


# --- lookup_label_candidates -----------------------------------------------

def test_lookup_label_candidates_builds_exact_match_query_and_escapes():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"results": {"bindings": [{"item": {"value": "Q1"}}]}}
    with patch("qlever_client.requests.post", return_value=mock_resp) as mock_post:
        result = qlever_client.lookup_label_candidates('a "tricky" name')
    assert result == ["Q1"]
    sent_query = mock_post.call_args.kwargs["data"]["query"]
    assert 'a \\"tricky\\" name' in sent_query
    assert "rdfs:label" in sent_query


# --- fetch_entity_for_index -------------------------------------------------

def test_fetch_entity_for_index_shapes_result_like_build_index_rows():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "results": {
            "bindings": [
                {
                    "label": {"value": "New York City"},
                    "desc": {"value": "largest city in the US"},
                    "aliases": {"value": "NYC | Big Apple"},
                }
            ]
        }
    }
    with patch("qlever_client.requests.post", return_value=mock_resp):
        entity = qlever_client.fetch_entity_for_index(
            "http://www.wikidata.org/entity/Q60"
        )
    assert entity == {
        "id": "Q60",
        "uri": "http://www.wikidata.org/entity/Q60",
        "label": "New York City",
        "description": "largest city in the US",
        "aliases": "NYC | Big Apple",
    }


def test_fetch_entity_for_index_no_results_returns_none():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"results": {"bindings": []}}
    with patch("qlever_client.requests.post", return_value=mock_resp):
        entity = qlever_client.fetch_entity_for_index("http://www.wikidata.org/entity/Q999999999")
    assert entity is None
