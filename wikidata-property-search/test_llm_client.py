"""Control-flow tests for llm_client.parse_permutations -- no HTTP calls."""
import llm_client


def test_non_json_text_returns_empty_list():
    assert llm_client.parse_permutations("I cannot answer that.") == []


def test_json_but_not_a_list_returns_empty_list():
    assert llm_client.parse_permutations('{"answer": "occupation"}') == []


def test_list_with_non_string_items_filters_them_out():
    result = llm_client.parse_permutations('["occupation", 42, null, "employer"]')
    assert result == ["occupation", "employer"]


def test_markdown_fenced_json_is_stripped_and_parsed():
    raw = '```json\n["occupation", "employer"]\n```'
    assert llm_client.parse_permutations(raw) == ["occupation", "employer"]


def test_markdown_fenced_json_without_language_tag():
    raw = '```\n["occupation"]\n```'
    assert llm_client.parse_permutations(raw) == ["occupation"]


def test_empty_string_returns_empty_list():
    assert llm_client.parse_permutations("") == []


def test_whitespace_only_string_returns_empty_list():
    assert llm_client.parse_permutations("   \n  ") == []


def test_valid_json_list_of_strings_returned_in_order():
    raw = '["occupation", "employer", "field of work"]'
    assert llm_client.parse_permutations(raw) == ["occupation", "employer", "field of work"]


def test_truncates_to_max_items():
    raw = '["a", "b", "c", "d", "e"]'
    assert llm_client.parse_permutations(raw, max_items=3) == ["a", "b", "c"]


def test_blank_strings_in_list_are_filtered():
    raw = '["occupation", "", "  ", "employer"]'
    assert llm_client.parse_permutations(raw) == ["occupation", "employer"]


# --- parse_relation_pairs ---------------------------------------------------

def test_relation_pairs_valid_list_returned_in_order():
    raw = '[["award received", "Nobel Prize in Physics"], ["nominated for", "Nobel Prize in Physics"]]'
    assert llm_client.parse_relation_pairs(raw) == [
        ("award received", "Nobel Prize in Physics"),
        ("nominated for", "Nobel Prize in Physics"),
    ]


def test_relation_pairs_non_json_text_returns_empty_list():
    assert llm_client.parse_relation_pairs("I cannot answer that.") == []


def test_relation_pairs_json_but_not_a_list_returns_empty_list():
    assert llm_client.parse_relation_pairs('{"property": "award received"}') == []


def test_relation_pairs_malformed_entries_dropped_not_whole_list():
    raw = '[["award received", "Nobel Prize in Physics"], ["only one element"], "not a pair", ["a", 42], ["b", "c"]]'
    assert llm_client.parse_relation_pairs(raw) == [
        ("award received", "Nobel Prize in Physics"),
        ("b", "c"),
    ]


def test_relation_pairs_empty_string_returns_empty_list():
    assert llm_client.parse_relation_pairs("") == []


def test_relation_pairs_markdown_fenced_json_is_stripped_and_parsed():
    raw = '```json\n[["award received", "Nobel Prize in Physics"]]\n```'
    assert llm_client.parse_relation_pairs(raw) == [("award received", "Nobel Prize in Physics")]


def test_relation_pairs_truncates_to_max_items():
    raw = '[["a", "1"], ["b", "2"], ["c", "3"]]'
    assert llm_client.parse_relation_pairs(raw, max_items=2) == [("a", "1"), ("b", "2")]


def test_generate_relation_pairs_calls_chat_complete_with_relation_prompt(monkeypatch):
    captured = {}

    def fake_chat_complete(system_prompt, user_phrase, **kwargs):
        captured["system_prompt"] = system_prompt
        captured["user_phrase"] = user_phrase
        return '[["award received", "Nobel Prize in Physics"]]'

    monkeypatch.setattr(llm_client, "chat_complete", fake_chat_complete)

    result = llm_client.generate_relation_pairs("received the Nobel Prize in Physics")

    assert result == [("award received", "Nobel Prize in Physics")]
    assert captured["user_phrase"] == "received the Nobel Prize in Physics"
    assert "property_label" in captured["system_prompt"]
