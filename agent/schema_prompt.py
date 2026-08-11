"""The stable, cached system prompt assembled from agent/prompts Markdown.

It must not vary per request, and its schema content must stay aligned with the
COMMENT metadata in db/migrations/0001.
"""
from __future__ import annotations

from .prompts import load, render

SCHEMA_DDL = load("schema")
SEMANTICS = load("semantics")
GOTCHAS = load("gotchas")
RULES = load("rules")
OUTCOME_GUIDANCE = load("outcome_guidance")

SYSTEM_PROMPT = render(
    "system",
    schema=SCHEMA_DDL,
    semantics=SEMANTICS,
    gotchas=GOTCHAS,
    rules=RULES,
    outcome_guidance=OUTCOME_GUIDANCE,
)


def system_blocks(cache: bool = True) -> list[dict]:
    """The system parameter, with the cache breakpoint at the end of the prefix."""
    block: dict = {"type": "text", "text": SYSTEM_PROMPT}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]
