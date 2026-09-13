"""Tests for server.py's SERVICE-body parsing: brace-depth extraction, the
?var -> Turtle-placeholder substitution, and all three request shapes
(legacy single-search, batch, relation pair).

Importing server.py runs its module-level index-loading code against the
real index/ and index_items/ directories -- that's fine here since we don't
mutate them, but it does mean resolution helpers below stick to phrases that
are exact tier-1 hits in the real index, so no tier-3/network calls happen
incidentally in these tests."""
from index_store import IndexBundle
import server


# --- search_lexical -----------------------------------------------------------

def test_search_lexical_disambiguates_item_ties_via_live_notability(monkeypatch):
    """Regression test: multiple items can share the exact same label
    (unlike properties). Tier 2 used to break such ties by semantic score;
    with it gone, tier 1 must fall back to the same live-notability signal
    tier-3 already uses."""
    meta = [
        {"qid": "Q1", "uri": "u1", "label": "city", "description": "niche def", "aliases": ""},
        {"qid": "Q515", "uri": "u515", "label": "city", "description": "large human settlement", "aliases": ""},
    ]
    bundle = IndexBundle(meta=meta, lex_index={"city": [0, 1]})

    monkeypatch.setattr(
        server.qlever_client, "rank_by_local_notability", lambda uris: "u515"
    )

    hits = server.search_lexical("city", 1, bundle, ptype="item")

    assert hits == [meta[1]]


def test_search_lexical_skips_notability_call_for_single_match(monkeypatch):
    meta = [{"qid": "Q515", "uri": "u515", "label": "city", "description": "", "aliases": ""}]
    bundle = IndexBundle(meta=meta, lex_index={"city": [0]})

    def should_not_be_called(uris):
        raise AssertionError("should not be called")

    monkeypatch.setattr(server.qlever_client, "rank_by_local_notability", should_not_be_called)

    hits = server.search_lexical("city", 1, bundle, ptype="item")

    assert hits == [meta[0]]


def test_search_lexical_skips_notability_call_for_properties():
    meta = [
        {"pid": "P1", "uri": "u1", "label": "x", "description": "", "aliases": ""},
        {"pid": "P2", "uri": "u2", "label": "x", "description": "", "aliases": ""},
    ]
    bundle = IndexBundle(meta=meta, lex_index={"x": [0, 1]})

    # No ptype="item" -> no live disambiguation attempted; first row wins as-is.
    hits = server.search_lexical("x", 1, bundle)

    assert hits == [meta[0]]


# --- _extract_service_body ---------------------------------------------------

def test_extract_service_body_finds_block():
    query = 'SELECT * WHERE { SERVICE <http://localhost:7002/sparql> { bd:x mwapi:y "z" . } }'
    body = server._extract_service_body(query)
    assert body.strip() == 'bd:x mwapi:y "z" .'


def test_extract_service_body_handles_nested_braces():
    query = (
        "SELECT * WHERE { SERVICE <http://localhost:7002/sparql> { "
        '[] mwapi:search "x" ; mwapi:bind ?a . '
        "} FILTER(?a = ?b) }"
    )
    body = server._extract_service_body(query)
    assert "mwapi:search" in body
    assert "FILTER" not in body


def test_extract_service_body_returns_empty_when_no_braces_at_all():
    assert server._extract_service_body("ASK { }".replace("{ }", "")) == ""


def test_extract_service_body_falls_back_to_first_brace_when_no_service_wrapper():
    """This is QLever's REAL wire format: it never forwards the client's
    original `SERVICE <url> { ... }` syntax -- it sends us a standalone
    `PREFIX ... SELECT <vars> { <graph pattern> }` query it synthesizes
    itself (no "WHERE" keyword either, since that's optional and QLever's
    serializer omits it)."""
    query = 'PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>\nSELECT ?popProp {\n  bd:serviceParam mwapi:search "population" .\n  ?popProp wikibase:apiOutput mwapi:directProperty .\n}'
    body = server._extract_service_body(query)
    assert 'mwapi:search "population"' in body
    assert "SELECT" not in body


# --- _extract_expected_vars ---------------------------------------------------

def test_extract_expected_vars_from_qlever_wire_format():
    """The real format QLever sends: no SERVICE wrapper, no WHERE keyword,
    an explicit SELECT var list that may include synthetic names for blank
    nodes in the original client query (?_QLever_internal_variable_N)."""
    query = (
        "PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>\n"
        "SELECT ?_QLever_internal_variable_1 ?birthProp ?_QLever_internal_variable_2 ?deathProp {\n"
        '  [] mwapi:search "date of birth" ; mwapi:bind ?birthProp .\n'
        '  [] mwapi:search "date of death" ; mwapi:bind ?deathProp .\n'
        "}"
    )
    assert server._extract_expected_vars(query) == [
        "_QLever_internal_variable_1", "birthProp", "_QLever_internal_variable_2", "deathProp",
    ]


def test_extract_expected_vars_returns_empty_for_select_star():
    query = "SELECT * WHERE { bd:serviceParam mwapi:search \"population\" . }"
    assert server._extract_expected_vars(query) == []


def test_parse_service_body_carries_expected_vars_through():
    query = (
        "PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>\n"
        "SELECT ?_QLever_internal_variable_1 ?birthProp {\n"
        '  [] mwapi:search "date of birth" ; mwapi:bind ?birthProp .\n'
        "}"
    )
    parsed = server.parse_service_body(query)
    assert parsed.expected_vars == ["_QLever_internal_variable_1", "birthProp"]


# --- _to_turtle / _varname ----------------------------------------------------

def test_to_turtle_substitutes_variables():
    turtle = server._to_turtle('?prop wikibase:apiOutput mwapi:directProperty .')
    assert "?prop" not in turtle
    assert "<urn:var:prop>" in turtle


def test_varname_recognizes_placeholder_and_rejects_other():
    from rdflib import URIRef
    assert server._varname(URIRef("urn:var:birthProp")) == "birthProp"
    assert server._varname(URIRef("http://example.org/x")) is None


# --- parse_service_body: legacy form -----------------------------------------

def test_parse_service_body_legacy_form():
    query = """
    PREFIX wikibase: <http://wikiba.se/ontology#>
    PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>
    PREFIX bd: <http://www.bigdata.com/rdf#>
    SELECT * WHERE {
      SERVICE <http://localhost:7002/sparql> {
        bd:serviceParam mwapi:search "date of birth" .
        bd:serviceParam mwapi:type "property" .
        bd:serviceParam wikibase:limit "3" .
        ?prop wikibase:apiOutputItem mwapi:item .
        ?label wikibase:apiOutput mwapi:label .
        ?score wikibase:apiOrdinal true .
      }
    }
    """
    parsed = server.parse_service_body(query)
    assert parsed.legacy == {
        "phrase": "date of birth",
        "type": "property",
        "limit": 3,
        "item_var": "prop",
        "direct_var": None,
        "label_var": "label",
        "score_var": "score",
    }
    assert parsed.batches == []
    assert parsed.relations == []


def test_parse_service_body_legacy_defaults_when_type_and_limit_omitted():
    query = """
    PREFIX wikibase: <http://wikiba.se/ontology#>
    PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>
    PREFIX bd: <http://www.bigdata.com/rdf#>
    SELECT * WHERE {
      SERVICE <http://localhost:7002/sparql> {
        bd:serviceParam mwapi:search "population" .
        ?prop wikibase:apiOutput mwapi:directProperty .
      }
    }
    """
    parsed = server.parse_service_body(query)
    assert parsed.legacy["phrase"] == "population"
    assert parsed.legacy["type"] is None
    assert parsed.legacy["limit"] == server.DEFAULT_LIMIT
    assert parsed.legacy["direct_var"] == "prop"


def test_parse_service_body_empty_when_no_search_phrase():
    query = "SELECT * WHERE { SERVICE <http://localhost:7002/sparql> { } }"
    parsed = server.parse_service_body(query)
    assert parsed.legacy is None
    assert parsed.batches == []
    assert parsed.relations == []


# --- parse_service_body: batch form ------------------------------------------

def test_parse_service_body_batch_form():
    query = """
    PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>
    SELECT * WHERE {
      SERVICE <http://localhost:7002/sparql> {
        [] mwapi:search "date of birth" ; mwapi:type "property" ; mwapi:bind ?birthProp .
        [] mwapi:search "date of death" ; mwapi:type "property" ; mwapi:bind ?deathProp .
      }
    }
    """
    parsed = server.parse_service_body(query)
    assert parsed.legacy is None
    assert parsed.relations == []
    assert {"phrase": "date of birth", "type": "property", "bind_var": "birthProp"} in parsed.batches
    assert {"phrase": "date of death", "type": "property", "bind_var": "deathProp"} in parsed.batches
    assert len(parsed.batches) == 2


def test_parse_service_body_batch_form_defaults_type_to_property():
    query = """
    PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>
    SELECT * WHERE {
      SERVICE <http://localhost:7002/sparql> {
        [] mwapi:search "city" ; mwapi:bind ?cityClass .
      }
    }
    """
    parsed = server.parse_service_body(query)
    assert parsed.batches == [{"phrase": "city", "type": "property", "bind_var": "cityClass"}]


# --- parse_service_body: relation form ----------------------------------------

def test_parse_service_body_relation_form():
    query = """
    PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>
    SELECT * WHERE {
      SERVICE <http://localhost:7002/sparql> {
        [] mwapi:searchRelation "received the Nobel Prize in Physics" ;
           mwapi:bindProperty ?awardProp ;
           mwapi:bindItem ?nobelPhysics .
      }
    }
    """
    parsed = server.parse_service_body(query)
    assert parsed.legacy is None
    assert parsed.batches == []
    assert parsed.relations == [{
        "phrase": "received the Nobel Prize in Physics",
        "property_var": "awardProp",
        "item_var": "nobelPhysics",
    }]


def test_parse_service_body_mixed_batch_and_relation_in_one_call():
    query = """
    PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>
    SELECT * WHERE {
      SERVICE <http://localhost:7002/sparql> {
        [] mwapi:searchRelation "instance of brewery" ;
           mwapi:bindProperty ?instanceOfProp ;
           mwapi:bindItem ?breweryClass .
        [] mwapi:search "inception" ; mwapi:type "property" ; mwapi:bind ?inceptionProp .
      }
    }
    """
    parsed = server.parse_service_body(query)
    assert parsed.legacy is None
    assert len(parsed.batches) == 1
    assert len(parsed.relations) == 1


# --- /sparql route: mixed-forms rejection and dispatch ------------------------

def test_sparql_route_rejects_mixed_legacy_and_batch_forms():
    client = server.app.test_client()
    query = """
    PREFIX bd: <http://www.bigdata.com/rdf#>
    PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>
    SELECT * WHERE {
      SERVICE <http://localhost:7002/sparql> {
        bd:serviceParam mwapi:search "date of birth" .
        bd:serviceParam mwapi:type "property" .
        [] mwapi:search "date of death" ; mwapi:type "property" ; mwapi:bind ?deathProp .
      }
    }
    """
    resp = client.get("/sparql", query_string={"query": query})
    assert resp.status_code == 400


def test_sparql_route_batch_form_resolves_known_property():
    """"date of birth" is a real tier-1 hit in the base index, so this
    exercises the full batch dispatch path with zero network calls."""
    client = server.app.test_client()
    query = """
    PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>
    SELECT * WHERE {
      SERVICE <http://localhost:7002/sparql> {
        [] mwapi:search "date of birth" ; mwapi:type "property" ; mwapi:bind ?birthProp .
      }
    }
    """
    resp = client.get("/sparql", query_string={"query": query})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["head"]["vars"] == ["birthProp"]
    assert body["results"]["bindings"][0]["birthProp"]["value"] == "http://www.wikidata.org/prop/direct/P569"


def test_sparql_route_legacy_form_missing_phrase_returns_empty():
    client = server.app.test_client()
    query = "SELECT * WHERE { SERVICE <http://localhost:7002/sparql> { } }"
    resp = client.get("/sparql", query_string={"query": query})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"head": {"vars": []}, "results": {"bindings": []}}
