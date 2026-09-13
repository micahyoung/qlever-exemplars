#!/usr/bin/env python3
"""Shared index storage: the IndexBundle type, base-index loading, and the
base/learned-overlay split for tier-3-persisted data.

Each index has a base file, and a learned-overlay file:
  - "base": meta.json, written ONLY by build_index.py. Frozen between
    builds -- nothing at runtime ever mutates this.
  - "learned overlay": a small, separate file that tier-3 resolution appends
    to. Merged with the base at load time into one in-memory IndexBundle, so
    the read path (server.py's lexical lookup) never needs to know the split
    exists.

This separation exists because conflating the two in one file (the original
design) made it impossible to tell which rows/aliases were part of the
original build vs. added later by an LLM at runtime -- which meant a bad
tier-3 resolution could only be found and undone via manual grep-and-repair.
With the split, "what did tier-3 add" is just reading a small file, and
"undo it" is `git checkout`/`rm` on that file, never touching the base data.

No Flask, no HTTP calls -- pure file I/O, safe to import in tests.
"""
import json
import os
import re
from collections import namedtuple


def normalize(text):
    """Lowercase + collapse whitespace for exact lexical matching."""
    return re.sub(r"\s+", " ", text.strip().lower())


IndexBundle = namedtuple("IndexBundle", ["meta", "lex_index"])


def _index_rows(lex_index, meta_rows, start_index):
    """Extend `lex_index` (normalized label/alias -> row indices) in place
    for `meta_rows`, whose absolute positions begin at `start_index`. Shared
    by _build_lex_index (indexing everything from 0) and
    merge_learned_entities (indexing only the appended tail).

    Two passes -- all labels, then all aliases -- so that when a phrase
    matches one row's LABEL and a different row's ALIAS (e.g. "capital" is
    P36's label but also a listed alias of P1376 "capital of"), the label
    match always lands first in that phrase's row list. This is the only
    per-phrase disambiguation signal available now that tier 2's
    embedding-based ranking (which used to reorder lexical pins by semantic
    score) is gone -- without it, a phrase with a tied alias/label match
    would resolve to whichever row happened to be indexed first, which is
    arbitrary and was observed to pick the wrong property in practice."""
    for offset, m in enumerate(meta_rows):
        i = start_index + offset
        lex_index.setdefault(normalize(m["label"]), []).append(i)
    for offset, m in enumerate(meta_rows):
        i = start_index + offset
        for a in m.get("aliases", "").split(" | "):
            if a:
                lex_index.setdefault(normalize(a), []).append(i)
    return lex_index


def _build_lex_index(meta):
    """normalized label/alias -> list of row indices, for the full meta list."""
    return _index_rows({}, meta, 0)


def _apply_alias(meta_row, alias):
    """Return a copy of `meta_row` with `alias` appended to its aliases
    string (pipe-joined, de-duplicated). Shared by merge_learned_aliases
    (applying the whole overlay at load time) and append_learned_alias
    (applying one new alias at persist time), so the two paths can't drift."""
    row = dict(meta_row)
    existing = [a for a in row.get("aliases", "").split(" | ") if a]
    if alias not in existing:
        existing.append(alias)
    row["aliases"] = " | ".join(existing)
    return row


def normalize_entity_row(raw, id_key):
    """Rename a fetch_entities()-shaped dict's "id" key to `id_key`
    ("pid"/"qid"). This is the single source of truth for that rename --
    both build_index.build_and_save (base build) and
    append_learned_entity (tier-3 persistence) call this, so neither can
    drift from the other the way they used to (tier-3-added item rows used
    to keep the raw "id" key instead of "qid", inconsistent with every
    base-built row)."""
    return {
        id_key: raw["id"],
        "uri": raw["uri"],
        "label": raw["label"],
        "description": raw["description"],
        "aliases": raw["aliases"],
    }


def load_index_bundle(dir_path, required):
    """Load the BASE meta.json from dir_path into an IndexBundle (no learned
    overlay). Returns None (instead of raising) if the file doesn't exist
    and `required` is False, so the server can still start and serve the
    other index while e.g. the item index hasn't been built yet.
    """
    meta_path = os.path.join(dir_path, "meta.json")
    if not os.path.exists(meta_path):
        if required:
            raise FileNotFoundError(f"required index missing at {dir_path}")
        print(f"WARNING: index not found at {dir_path}, skipping", flush=True)
        return None

    with open(meta_path) as f:
        meta = json.load(f)

    # Lexical index: normalized label/alias -> row indices.
    return IndexBundle(meta, _build_lex_index(meta))


def atomic_save_json(path, obj):
    """Write `obj` as JSON to `path` atomically via a temp file + os.replace."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


# --- learned overlay: properties ------------------------------------------

def load_learned_aliases(path):
    """Load the property learned-overlay file: a list of
    {"pid", "alias", "added_at", "source_model"} records. Returns [] if the
    file doesn't exist yet (the default state for a fresh checkout, or
    before tier-3 has ever succeeded)."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def save_learned_aliases(path, records):
    atomic_save_json(path, records)


def merge_learned_aliases(bundle, learned_records):
    """Pure function, no I/O: apply each learned alias record onto its
    target base row (found by "pid"), returning a new IndexBundle. Records
    referencing a pid absent from `bundle` (base/overlay drift -- shouldn't
    happen, but must never crash the server) are skipped with a warning."""
    if not learned_records:
        return bundle

    pid_to_row = {m.get("pid"): i for i, m in enumerate(bundle.meta) if "pid" in m}
    new_meta = list(bundle.meta)
    new_lex_index = {k: list(v) for k, v in bundle.lex_index.items()}

    for record in learned_records:
        row_index = pid_to_row.get(record["pid"])
        if row_index is None:
            print(f"WARNING: learned alias references unknown pid {record['pid']!r}, skipping", flush=True)
            continue
        new_meta[row_index] = _apply_alias(new_meta[row_index], record["alias"])
        norm_alias = normalize(record["alias"])
        new_lex_index.setdefault(norm_alias, [])
        if row_index not in new_lex_index[norm_alias]:
            new_lex_index[norm_alias].append(row_index)

    return IndexBundle(new_meta, new_lex_index)


def append_learned_alias(bundle, learned_aliases_path, row_index, alias, source_model, lock):
    """Property tier-3 success: persist `alias` as a new record in the
    (small) learned-overlay file, and apply it to the in-memory bundle.
    Never touches the base meta.json.

    Copy-on-write: never mutates `bundle` in place. Returns
    (new_bundle, new_learned_records_list); the caller swaps both into
    whatever module-level globals hold the live state.
    """
    norm_alias = normalize(alias)
    with lock:
        # Idempotency guard: a concurrent duplicate request may have already
        # persisted this exact alias while this one was waiting on the LLM
        # round-trip (which can take up to tens of seconds).
        if row_index in bundle.lex_index.get(norm_alias, []):
            return bundle, None  # caller should keep its existing learned list

        pid = bundle.meta[row_index]["pid"]
        record = {
            "pid": pid,
            "alias": alias,
            "added_at": _now_iso(),
            "source_model": source_model,
        }

        existing_records = load_learned_aliases(learned_aliases_path)
        new_records = existing_records + [record]
        save_learned_aliases(learned_aliases_path, new_records)

        new_meta = list(bundle.meta)
        new_meta[row_index] = _apply_alias(new_meta[row_index], alias)
        new_lex_index = {k: list(v) for k, v in bundle.lex_index.items()}
        new_lex_index.setdefault(norm_alias, [])
        if row_index not in new_lex_index[norm_alias]:
            new_lex_index[norm_alias].append(row_index)

        new_bundle = IndexBundle(new_meta, new_lex_index)
        return new_bundle, new_records


# --- learned overlay: items -------------------------------------------------

def load_learned_entities(meta_path):
    """Load the item learned-overlay file. Returns [] if it's missing
    (fresh checkout / no items resolved yet)."""
    if not os.path.exists(meta_path):
        return []
    with open(meta_path) as f:
        return json.load(f)


def merge_learned_entities(bundle, learned_meta):
    """Pure function, no I/O: append learned item rows after the base rows.
    Identity no-op when the overlay is empty (the common case until tier-3
    resolves its first new entity)."""
    if not learned_meta:
        return bundle

    new_meta = list(bundle.meta) + list(learned_meta)
    new_lex_index = {k: list(v) for k, v in bundle.lex_index.items()}
    _index_rows(new_lex_index, learned_meta, start_index=len(bundle.meta))

    return IndexBundle(new_meta, new_lex_index)


def append_learned_entity(bundle, learned_meta_path, raw_entity, source_model, lock):
    """Item tier-3 success: persist a brand-new entity into the (small)
    learned-overlay file, and append it to the in-memory bundle. Never
    touches the base meta.json.

    `raw_entity` is in the pre-rename shape resolver.py already produces
    (key "id", matching qlever_client.fetch_entity_for_index /
    build_index.fetch_entities) -- normalize_entity_row() fixes the key
    here, at the one place item rows get persisted at runtime, rather than
    leaving it to drift as it did before.

    Copy-on-write. Returns (new_bundle, new_learned_meta); None-paired if
    this was a no-op duplicate.
    """
    norm_label = normalize(raw_entity["label"])
    with lock:
        # Idempotency guard, scoped to an exact label+uri match.
        for idx in bundle.lex_index.get(norm_label, []):
            if bundle.meta[idx].get("uri") == raw_entity.get("uri"):
                return bundle, None

        entity = normalize_entity_row(raw_entity, id_key="qid")
        entity["added_at"] = _now_iso()
        entity["source_model"] = source_model

        existing_meta = load_learned_entities(learned_meta_path)
        new_learned_meta = existing_meta + [entity]
        atomic_save_json(learned_meta_path, new_learned_meta)

        new_meta = list(bundle.meta) + [entity]
        new_row_index = len(new_meta) - 1
        new_lex_index = {k: list(v) for k, v in bundle.lex_index.items()}
        surface = [entity["label"]] + [a for a in entity.get("aliases", "").split(" | ") if a]
        for s in surface:
            new_lex_index.setdefault(normalize(s), []).append(new_row_index)

        new_bundle = IndexBundle(new_meta, new_lex_index)
        return new_bundle, new_learned_meta


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
