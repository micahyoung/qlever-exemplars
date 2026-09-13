#!/usr/bin/env python3
"""One-time migration: move the tier-3-added data that's currently mixed
into the base index files out into the dedicated learned-overlay files.

Run once, from wikidata-property-search/:
    .venv/bin/python scripts/migrate_learned_overlay.py

Since there's no programmatic way to distinguish tier-3-added data from
base data before this migration runs (that's the bug this whole change
fixes), this script hardcodes the exact known-good list, identified by hand
during the incident audit that prompted this change. Idempotent: re-running
skips entries already present in the learned-overlay files.

BACK UP index/ and index_items/ before running this -- it rewrites
index/meta.json, index_items/meta.json, and index_items/vectors.npy in
place, and there is no other copy of that data.
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from index_store import atomic_save_json, atomic_save_npy, normalize_entity_row

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_DIR = os.path.join(HERE, "index")
ITEM_INDEX_DIR = os.path.join(HERE, "index_items")

INDEX_META_PATH = os.path.join(INDEX_DIR, "meta.json")
ITEM_META_PATH = os.path.join(ITEM_INDEX_DIR, "meta.json")
ITEM_VECTORS_PATH = os.path.join(ITEM_INDEX_DIR, "vectors.npy")

LEARNED_ALIASES_PATH = os.path.join(INDEX_DIR, "learned_aliases.json")
LEARNED_ITEMS_META_PATH = os.path.join(ITEM_INDEX_DIR, "learned_entities.json")
LEARNED_ITEMS_VECTORS_PATH = os.path.join(ITEM_INDEX_DIR, "learned_vectors.npy")

EXPECTED_BASE_PROPERTY_ROWS = 13534
EXPECTED_BASE_ITEM_ROWS = 106452

KNOWN_PROPERTY_ALIASES = [
    ("P106", "gig they do for a living"),
    ("P169", "boss of a company"),
    ("P576", "when a country stopped existing"),
]
KNOWN_ITEM_QIDS = ["Q60", "Q1297", "Q312"]

SENTINEL_MODEL = "unknown (migrated pre-provenance)"
SENTINEL_TIME = "2026-09-13T00:00:00+00:00"


def migrate_properties():
    with open(INDEX_META_PATH) as f:
        meta = json.load(f)

    existing_learned = []
    if os.path.exists(LEARNED_ALIASES_PATH):
        with open(LEARNED_ALIASES_PATH) as f:
            existing_learned = json.load(f)
    already_migrated = {(r["pid"], r["alias"]) for r in existing_learned}

    new_learned = list(existing_learned)
    changed = False

    for pid, alias in KNOWN_PROPERTY_ALIASES:
        if (pid, alias) in already_migrated:
            print(f"  [skip] {pid} / {alias!r} already migrated")
            continue

        matches = [i for i, m in enumerate(meta) if m.get("pid") == pid]
        assert len(matches) == 1, f"expected exactly one row for {pid}, found {len(matches)}"
        row = meta[matches[0]]

        segments = [a for a in row["aliases"].split(" | ") if a]
        assert alias in segments, f"expected {alias!r} in {pid}'s aliases, not found"
        segments.remove(alias)
        row["aliases"] = " | ".join(segments)

        new_learned.append({
            "pid": pid,
            "alias": alias,
            "added_at": SENTINEL_TIME,
            "source_model": SENTINEL_MODEL,
        })
        changed = True
        print(f"  [migrate] {pid} / {alias!r} -> learned_aliases.json")

    assert len(meta) == EXPECTED_BASE_PROPERTY_ROWS, (
        f"property row count changed: {len(meta)} != {EXPECTED_BASE_PROPERTY_ROWS}"
    )

    if changed:
        atomic_save_json(INDEX_META_PATH, meta)
        atomic_save_json(LEARNED_ALIASES_PATH, new_learned)
        print(f"  wrote {INDEX_META_PATH} and {LEARNED_ALIASES_PATH}")
    else:
        print("  no property changes needed")


def migrate_items():
    with open(ITEM_META_PATH) as f:
        meta = json.load(f)
    vectors = np.load(ITEM_VECTORS_PATH)
    assert vectors.shape[0] == len(meta), "item vectors/meta length mismatch before migration"

    existing_meta = []
    existing_vectors = None
    if os.path.exists(LEARNED_ITEMS_META_PATH) and os.path.exists(LEARNED_ITEMS_VECTORS_PATH):
        with open(LEARNED_ITEMS_META_PATH) as f:
            existing_meta = json.load(f)
        existing_vectors = np.load(LEARNED_ITEMS_VECTORS_PATH)
    already_migrated = {m["qid"] for m in existing_meta}

    to_migrate_qids = [q for q in KNOWN_ITEM_QIDS if q not in already_migrated]
    if not to_migrate_qids:
        print("  no item changes needed (all already migrated)")
        assert len(meta) == EXPECTED_BASE_ITEM_ROWS, (
            f"item row count already at expected base size: {len(meta)} != {EXPECTED_BASE_ITEM_ROWS}"
        )
        return

    indices = []
    for qid in to_migrate_qids:
        matches = [i for i, m in enumerate(meta) if m.get("id") == qid or m.get("qid") == qid]
        assert len(matches) == 1, f"expected exactly one row for {qid}, found {len(matches)}"
        indices.append(matches[0])

    assert indices == sorted(indices), "expected known item rows to already be in order"
    assert indices[-1] - indices[0] == len(indices) - 1, (
        f"expected the {len(indices)} known item rows to be contiguous (trailing rows), got indices {indices}"
    )

    new_learned_meta = list(existing_meta)
    new_learned_vectors_list = [existing_vectors] if existing_vectors is not None else []

    for qid, idx in zip(to_migrate_qids, indices):
        raw = meta[idx]
        entity = normalize_entity_row(raw, id_key="qid")
        entity["added_at"] = SENTINEL_TIME
        entity["source_model"] = SENTINEL_MODEL
        new_learned_meta.append(entity)
        new_learned_vectors_list.append(vectors[idx][np.newaxis, :])
        print(f"  [migrate] {qid} ({raw.get('label')}) -> learned_entities.json (id->qid fixed)")

    new_learned_vectors = np.vstack(new_learned_vectors_list)

    keep_mask = np.ones(len(meta), dtype=bool)
    keep_mask[indices] = False
    new_base_meta = [m for i, m in enumerate(meta) if keep_mask[i]]
    new_base_vectors = vectors[keep_mask]

    assert len(new_base_meta) == EXPECTED_BASE_ITEM_ROWS, (
        f"base item row count after migration: {len(new_base_meta)} != {EXPECTED_BASE_ITEM_ROWS}"
    )
    assert new_base_vectors.shape[0] == EXPECTED_BASE_ITEM_ROWS
    assert len(new_learned_meta) == new_learned_vectors.shape[0]
    for m in new_learned_meta:
        assert "qid" in m and "id" not in m, f"learned item row still has 'id' key: {m}"

    atomic_save_json(ITEM_META_PATH, new_base_meta)
    atomic_save_npy(ITEM_VECTORS_PATH, new_base_vectors)
    atomic_save_json(LEARNED_ITEMS_META_PATH, new_learned_meta)
    atomic_save_npy(LEARNED_ITEMS_VECTORS_PATH, new_learned_vectors)
    print(f"  wrote {ITEM_META_PATH}, {ITEM_VECTORS_PATH}, {LEARNED_ITEMS_META_PATH}, {LEARNED_ITEMS_VECTORS_PATH}")


def main():
    print("Migrating property aliases...")
    migrate_properties()
    print("Migrating item rows...")
    migrate_items()
    print("Done.")


if __name__ == "__main__":
    main()
