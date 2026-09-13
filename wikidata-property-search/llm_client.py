#!/usr/bin/env python3
"""Tier-3 candidate generation: ask a local chat model to propose formal-name
permutations of a colloquial search phrase.

Two lessons learned from live prototyping, both load-bearing:

1. Several reasoning-capable chat models served at CHAT_URL (e.g. the
   qwen3.6/3.8 family) burn their entire token budget on invisible
   "thinking" tokens and return empty content -- even at max_tokens=1500
   (~38s wasted, no answer) -- unless `chat_template_kwargs:
   {"enable_thinking": false}` is set. This flag is sent unconditionally on
   every request; it's a no-op for models without a reasoning mode (verified
   against the current default, gemma-4-26b-a4b-vision) and load-bearing for
   ones that have it, so it's cheaper to always send it than to special-case
   per model.
2. The model's raw output is untrusted text, not a guaranteed-valid API
   response -- parse_permutations() must never raise on malformed output;
   a parse failure is just a tier-3 miss, not a request-breaking error.
"""
import json
import os
import re

import requests

CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8888/v1/chat/completions")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gemma-4-26b-a4b-vision")
CHAT_TIMEOUT = float(os.environ.get("CHAT_TIMEOUT", "120"))
CHAT_ENABLE_THINKING = os.environ.get("CHAT_ENABLE_THINKING", "false").lower() == "true"
MAX_PERMUTATIONS = int(os.environ.get("TIER3_MAX_PERMUTATIONS", "8"))

PROPERTY_PERMUTATION_PROMPT = (
    "You resolve casual, colloquial phrases into formal Wikidata property "
    "names. Given a phrase, output ONLY a JSON array of up to {n} short "
    "candidate Wikidata property labels, ordered from MOST to LEAST "
    "confident match (the kind of formal name you'd see in a Wikidata "
    'property list, e.g. "date of birth", "occupation", "country of '
    'citizenship"). No explanation, no markdown, just the JSON array.'
)

ITEM_PERMUTATION_PROMPT = (
    "You resolve casual references, nicknames, or informal descriptions "
    "into the formal, canonical name of the real-world entity. Given a "
    "phrase, output ONLY a JSON array of up to {n} short candidate "
    "canonical names, ordered from MOST to LEAST confident match (the kind "
    "of exact proper-noun name you'd see as a Wikipedia article title, e.g. "
    '"New York City", "Marie Curie", "Microsoft"). No explanation, no '
    "markdown, just the JSON array."
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def chat_complete(
    system_prompt,
    user_phrase,
    *,
    url=None,
    model=None,
    timeout=None,
    enable_thinking=None,
    max_tokens=300,
    temperature=0.3,
):
    """POST a single-turn chat completion request. Returns the raw message
    content string. Raises requests exceptions (timeout, connection error,
    HTTP error) -- callers decide whether that's a tier-3 miss or an error."""
    resp = requests.post(
        url or CHAT_URL,
        json={
            "model": model or CHAT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_phrase},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "chat_template_kwargs": {
                "enable_thinking": CHAT_ENABLE_THINKING
                if enable_thinking is None
                else enable_thinking
            },
        },
        timeout=timeout if timeout is not None else CHAT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_permutations(raw_text, max_items=MAX_PERMUTATIONS):
    """Best-effort extraction of a list[str] from LLM output. Handles
    markdown-code-fenced JSON. Never raises -- returns [] on any failure
    (empty/non-JSON/wrong-shaped output), filtering out non-string items
    rather than discarding the whole list for one bad entry."""
    if not raw_text or not raw_text.strip():
        return []
    text = _FENCE_RE.sub("", raw_text.strip()).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    strings = [item for item in data if isinstance(item, str) and item.strip()]
    return strings[:max_items]


def generate_property_permutations(phrase, n=MAX_PERMUTATIONS, **kwargs):
    raw = chat_complete(PROPERTY_PERMUTATION_PROMPT.format(n=n), phrase, **kwargs)
    return parse_permutations(raw, max_items=n)


def generate_item_permutations(phrase, n=MAX_PERMUTATIONS, **kwargs):
    raw = chat_complete(ITEM_PERMUTATION_PROMPT.format(n=n), phrase, **kwargs)
    return parse_permutations(raw, max_items=n)
