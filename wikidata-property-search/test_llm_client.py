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
