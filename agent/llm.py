"""Model-calling mechanics, shared by every node.

Owns only "how to call Claude for this node" -- model/effort selection
(agent/models.py) and prompt caching (agent/schema_prompt.py's cached system
prefix, reused rather than duplicated per the schema-text rule in the phase
plan). Each node's actual prompt content and structured-output schema is step
6's job; this module exists so step 6 doesn't also have to re-solve SDK
plumbing (json_schema output_config, effort placement, cache blocks) per node.

`ModelClient` is the seam: agent/graph.py takes one in and hands it to every
node's `build()`, so tests substitute a scripted fake without touching a
single node's code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelReply:
    payload: dict
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int


class ModelClient(Protocol):
    """The one method every node calls.

    Implementations must not raise for an ordinary model refusal or malformed
    reply -- deciding whether that is retryable is a node's job (it has the
    retry-count context), not this seam's. A raised exception here is reserved
    for genuine call failures (network, auth), same convention as
    evals.harness.envelope.Agent.
    """

    def complete(self, node: str, *, messages: list[dict], schema: dict) -> ModelReply: ...


class AnthropicModelClient:
    """Real implementation, built against `anthropic` per the claude-api skill.

    Construction never raises even with no credentials configured -- a bare
    `anthropic.Anthropic()` resolves an `ant auth login` profile if one exists
    and otherwise defers the failure to the first actual call. That matters
    here because this class gets constructed once per /agent request; it must
    not be why /health (which never touches it) needs credentials.
    """

    def __init__(self, api_key: str | None = None) -> None:
        import anthropic

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

    def complete(self, node: str, *, messages: list[dict], schema: dict) -> ModelReply:
        from .models import get_model_config
        from .schema_prompt import system_blocks

        config = get_model_config(node)
        kwargs: dict = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "system": system_blocks(cache=True),
            "messages": messages,
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if config.effort:
            kwargs["output_config"]["effort"] = config.effort

        response = self._client.messages.create(**kwargs)

        if response.stop_reason == "refusal":
            raise RuntimeError(f"model refused the request on node {node!r}")

        text = next((b.text for b in response.content if b.type == "text"), "")
        payload = json.loads(text) if text else {}
        usage = response.usage
        return ModelReply(
            payload=payload,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
