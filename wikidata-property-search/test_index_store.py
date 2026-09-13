"""Tests for index_store.py's atomic-write, base/learned-overlay merge, and
append primitives, against real (but ephemeral, pytest-managed) temp files
-- never the repo's real index/ or index_items/ directories."""
import json
import threading

import numpy as np
import pytest

import index_store
from index_store import (
    IndexBundle,
    append_learned_alias,
    append_learned_entity,
    atomic_save_json,
    atomic_save_npy,
    load_learned_aliases,
    load_learned_entities,
    merge_learned_aliases,
    merge_learned_entities,
    normalize_entity_row,
)


def test_atomic_save_npy_writes_correct_contents_and_cleans_up_temp(tmp_path):
    path = tmp_path / "vectors.npy"
    arr = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)

    atomic_save_npy(str(path), arr)

    assert path.exists()
    loaded = np.load(str(path))
    np.testing.assert_array_equal(loaded, arr)
    # regression guard for the "vectors.npy.tmp.npy" double-extension quirk:
    # no leftover temp artifacts of any name pattern.
    leftovers = [p for p in tmp_path.iterdir() if p.name != "vectors.npy"]
    assert leftovers == []


def test_atomic_save_json_writes_correct_contents_and_cleans_up_temp(tmp_path):
    path = tmp_path / "meta.json"
    obj = [{"pid": "P106", "label": "occupation"}]

    atomic_save_json(str(path), obj)

    assert path.exists()
    assert json.loads(path.read_text()) == obj
    leftovers = [p for p in tmp_path.iterdir() if p.name != "meta.json"]
    assert leftovers == []


# --- normalize_entity_row --------------------------------------------------

def test_normalize_entity_row_renames_id_to_given_key():
    raw = {"id": "Q60", "uri": "uQ60", "label": "New York City", "description": "d", "aliases": "a"}
    result = normalize_entity_row(raw, id_key="qid")
    assert result == {"qid": "Q60", "uri": "uQ60", "label": "New York City", "description": "d", "aliases": "a"}
    assert "id" not in result


def test_normalize_entity_row_works_for_pid_too():
    raw = {"id": "P106", "uri": "u106", "label": "occupation", "description": "", "aliases": ""}
    result = normalize_entity_row(raw, id_key="pid")
    assert result["pid"] == "P106"
    assert "id" not in result


# --- load_learned_* on a fresh checkout (files missing) --------------------

def test_load_learned_aliases_returns_empty_when_file_missing(tmp_path):
    assert load_learned_aliases(str(tmp_path / "learned_aliases.json")) == []


def test_load_learned_entities_returns_empty_when_files_missing(tmp_path):
    meta, vectors = load_learned_entities(
        str(tmp_path / "learned_entities.json"), str(tmp_path / "learned_vectors.npy")
    )
    assert meta == []
    assert vectors is None


# --- merge_learned_aliases --------------------------------------------------

def _base_property_bundle():
    meta = [
        {"pid": "P106", "uri": "u106", "label": "occupation", "description": "", "aliases": "job"},
        {"pid": "P108", "uri": "u108", "label": "employer", "description": "", "aliases": ""},
    ]
    lex_index = {"occupation": [0], "job": [0], "employer": [1]}
    return IndexBundle(vectors=None, meta=meta, lex_index=lex_index, instruction="")


def test_merge_learned_aliases_bakes_aliases_into_base_rows_without_mutating_base_files():
    bundle = _base_property_bundle()
    learned = [
        {"pid": "P106", "alias": "gig they do for a living", "added_at": "t", "source_model": "m"},
        {"pid": "P108", "alias": "who pays someone", "added_at": "t", "source_model": "m"},
    ]

    merged = merge_learned_aliases(bundle, learned)

    assert "gig they do for a living" in merged.meta[0]["aliases"]
    assert "who pays someone" in merged.meta[1]["aliases"]
    assert merged.lex_index["gig they do for a living"] == [0]
    assert merged.lex_index["who pays someone"] == [1]

    # base bundle (as loaded from meta.json, before merge) is unmutated
    assert "gig they do for a living" not in bundle.meta[0]["aliases"]
    assert "who pays someone" not in bundle.meta[1]["aliases"]


def test_merge_learned_aliases_skips_unknown_pid_without_raising():
    bundle = _base_property_bundle()
    learned = [{"pid": "P999999", "alias": "nonexistent property", "added_at": "t", "source_model": "m"}]

    merged = merge_learned_aliases(bundle, learned)

    assert merged.meta == bundle.meta
    assert "nonexistent property" not in merged.lex_index


def test_merge_learned_aliases_empty_list_is_noop():
    bundle = _base_property_bundle()
    merged = merge_learned_aliases(bundle, [])
    assert merged is bundle


# --- merge_learned_entities --------------------------------------------------

def _base_item_bundle():
    meta = [{"qid": "Q5", "uri": "uQ5", "label": "human", "description": "", "aliases": ""}]
    lex_index = {"human": [0]}
    vectors = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
    return IndexBundle(vectors=vectors, meta=meta, lex_index=lex_index, instruction="")


def test_merge_learned_entities_appends_after_base_rows_preserving_indices():
    bundle = _base_item_bundle()
    learned_meta = [
        {"qid": "Q60", "uri": "uQ60", "label": "New York City", "description": "", "aliases": "the Big Apple"},
        {"qid": "Q1297", "uri": "uQ1297", "label": "Chicago", "description": "", "aliases": "the Windy City"},
    ]
    learned_vectors = np.array([[0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32)

    merged = merge_learned_entities(bundle, learned_meta, learned_vectors)

    assert merged.vectors.shape == (3, 3)
    assert len(merged.meta) == 3
    # base row stays at index 0
    assert merged.meta[0]["qid"] == "Q5"
    assert merged.lex_index["human"] == [0]
    # learned rows land at indices 1-2, in overlay order
    assert merged.meta[1]["qid"] == "Q60"
    assert merged.meta[2]["qid"] == "Q1297"
    assert merged.lex_index["new york city"] == [1]
    assert merged.lex_index["the big apple"] == [1]
    assert merged.lex_index["chicago"] == [2]
    assert merged.lex_index["the windy city"] == [2]

    # base bundle unmutated
    assert bundle.vectors.shape == (1, 3)
    assert len(bundle.meta) == 1


def test_merge_learned_entities_is_noop_when_overlay_absent():
    bundle = _base_item_bundle()
    merged = merge_learned_entities(bundle, [], None)
    assert merged is bundle


# --- append_learned_alias ---------------------------------------------------

def test_append_learned_alias_writes_overlay_and_updates_in_memory_lex_index(tmp_path):
    base_meta_path = tmp_path / "meta.json"
    learned_path = tmp_path / "learned_aliases.json"
    base_meta = _base_property_bundle().meta
    base_meta_path.write_text(json.dumps(base_meta))
    bundle = _base_property_bundle()
    lock = threading.Lock()

    new_bundle, new_learned = append_learned_alias(
        bundle, str(learned_path), 0, "gig they do for a living", "gemma-4-26b-a4b-vision", lock
    )

    # in-memory bundle reflects the new alias
    assert "gig they do for a living" in new_bundle.meta[0]["aliases"]
    assert new_bundle.lex_index["gig they do for a living"] == [0]

    # overlay file has the record, with provenance
    on_disk = json.loads(learned_path.read_text())
    assert on_disk == new_learned
    assert on_disk[0]["pid"] == "P106"
    assert on_disk[0]["alias"] == "gig they do for a living"
    assert on_disk[0]["source_model"] == "gemma-4-26b-a4b-vision"
    assert "added_at" in on_disk[0]

    # base meta.json is NEVER touched by this function
    assert json.loads(base_meta_path.read_text()) == base_meta

    # old bundle object (captured before the call) is unmutated
    assert "gig they do for a living" not in bundle.meta[0]["aliases"]
    assert "gig they do for a living" not in bundle.lex_index


def test_append_learned_alias_idempotent_on_duplicate_phrase(tmp_path):
    learned_path = tmp_path / "learned_aliases.json"
    bundle = _base_property_bundle()
    lock = threading.Lock()

    first_bundle, first_learned = append_learned_alias(
        bundle, str(learned_path), 0, "gig they do for a living", "model-a", lock
    )

    write_count = {"n": 0}
    real_atomic_save_json = index_store.atomic_save_json

    def counting_save(path, obj):
        write_count["n"] += 1
        real_atomic_save_json(path, obj)

    index_store.atomic_save_json = counting_save
    try:
        second_bundle, second_learned = append_learned_alias(
            first_bundle, str(learned_path), 0, "gig they do for a living", "model-a", lock
        )
    finally:
        index_store.atomic_save_json = real_atomic_save_json

    assert write_count["n"] == 0  # no-op: alias already present
    assert second_bundle is first_bundle
    assert second_learned is None  # signals caller to keep its existing list


# --- append_learned_entity ---------------------------------------------------

def test_append_learned_entity_writes_overlay_and_normalizes_id_key(tmp_path):
    base_meta_path = tmp_path / "meta.json"
    base_vectors_path = tmp_path / "vectors.npy"
    learned_meta_path = tmp_path / "learned_entities.json"
    learned_vectors_path = tmp_path / "learned_vectors.npy"

    bundle = _base_item_bundle()
    base_meta_path.write_text(json.dumps(bundle.meta))
    np.save(str(base_vectors_path), bundle.vectors)
    lock = threading.Lock()

    raw_entity = {"id": "Q60", "uri": "uQ60", "label": "New York City", "description": "", "aliases": "the Big Apple"}
    new_vector = np.array([0.0, 1.0, 0.0], dtype=np.float32)

    new_bundle, new_learned_meta, new_learned_vectors = append_learned_entity(
        bundle, str(learned_meta_path), str(learned_vectors_path), raw_entity, new_vector, "gemma-4-26b-a4b-vision", lock
    )

    # key was normalized "id" -> "qid" (the regression test for the drift bug)
    assert "qid" in new_bundle.meta[1]
    assert new_bundle.meta[1]["qid"] == "Q60"
    assert "id" not in new_bundle.meta[1]
    assert new_bundle.meta[1]["source_model"] == "gemma-4-26b-a4b-vision"
    assert "added_at" in new_bundle.meta[1]

    assert new_bundle.vectors.shape == (2, 3)
    np.testing.assert_array_equal(new_bundle.vectors[1], new_vector)
    assert new_bundle.lex_index["new york city"] == [1]
    assert new_bundle.lex_index["the big apple"] == [1]

    # overlay files have the new row
    on_disk_meta = json.loads(learned_meta_path.read_text())
    assert len(on_disk_meta) == 1
    assert on_disk_meta[0]["qid"] == "Q60"
    on_disk_vectors = np.load(str(learned_vectors_path))
    assert on_disk_vectors.shape == (1, 3)

    # base files are NEVER touched by this function
    assert json.loads(base_meta_path.read_text()) == bundle.meta
    on_disk_base_vectors = np.load(str(base_vectors_path))
    np.testing.assert_array_equal(on_disk_base_vectors, bundle.vectors)

    # old bundle unmutated
    assert bundle.vectors.shape == (1, 3)
    assert len(bundle.meta) == 1


def test_append_learned_entity_idempotent_on_duplicate_uri(tmp_path):
    learned_meta_path = tmp_path / "learned_entities.json"
    learned_vectors_path = tmp_path / "learned_vectors.npy"
    bundle = _base_item_bundle()
    lock = threading.Lock()

    raw_entity = {"id": "Q60", "uri": "uQ60", "label": "New York City", "description": "", "aliases": ""}
    vector = np.array([0.0, 1.0, 0.0], dtype=np.float32)

    first_bundle, first_meta, first_vectors = append_learned_entity(
        bundle, str(learned_meta_path), str(learned_vectors_path), raw_entity, vector, "m", lock
    )
    second_bundle, second_meta, second_vectors = append_learned_entity(
        first_bundle, str(learned_meta_path), str(learned_vectors_path), raw_entity, vector, "m", lock
    )

    assert second_bundle is first_bundle
    assert second_meta is None
    assert second_vectors is None
    assert second_bundle.vectors.shape == (2, 3)  # not appended twice
