#!/usr/bin/env python3
"""List everything tier-3 has persisted, read-only.

Answers "what has tier-3 added" directly from the learned-overlay files,
instead of grepping the base index (the failure mode that caused a bad
resolution to go unnoticed until it was hit by accident).

Usage: .venv/bin/python list_learned.py
"""
import os

from index_store import load_learned_aliases, load_learned_entities

HERE = os.path.dirname(os.path.abspath(__file__))
LEARNED_ALIASES_PATH = os.path.join(HERE, "index", "learned_aliases.json")
LEARNED_ITEMS_META_PATH = os.path.join(HERE, "index_items", "learned_entities.json")
LEARNED_ITEMS_VECTORS_PATH = os.path.join(HERE, "index_items", "learned_vectors.npy")


def _print_table(rows, headers):
    widths = [max(len(str(r[i])) for r in ([headers] + rows)) for i in range(len(headers))]
    fmt = " | ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(fmt.format(*r))


def main():
    aliases = load_learned_aliases(LEARNED_ALIASES_PATH)
    print(f"=== Learned property aliases ({len(aliases)}) ===")
    if aliases:
        _print_table(
            [[a["pid"], a["alias"], a["source_model"], a["added_at"]] for a in aliases],
            ["pid", "alias", "source_model", "added_at"],
        )
    else:
        print("(none)")

    meta, _ = load_learned_entities(LEARNED_ITEMS_META_PATH, LEARNED_ITEMS_VECTORS_PATH)
    print(f"\n=== Learned items ({len(meta)}) ===")
    if meta:
        _print_table(
            [[m["qid"], m["label"], m["source_model"], m["added_at"]] for m in meta],
            ["qid", "label", "source_model", "added_at"],
        )
    else:
        print("(none)")


if __name__ == "__main__":
    main()
