"""Zero-shot single-call baseline: the control arm.

Lives in the harness, not in `agent/`, because it is an eval *subject* rather
than service code -- nothing in the deployed graph imports it. Keeping it here
is also what lets `agent/` ship with no dependency on the eval harness at all.

One model call, schema in the prompt, one execution. No graph, no validator
feedback loop, no critic, no retry. This is deliberately the least sophisticated
thing that could work.

It exists because every claim Phase 4 wants to make is comparative. "The critic node
catches valid-but-wrong" and "rungs 4-5 are where the value is" are only meaningful
against a floor, and without one the ablation degenerates to "graph vs. nothing".
Each node added in step 6 is measured as a delta against these numbers.

It is also the reason step 1's harness is verifiable on day one: a harness with no
subject cannot be run, only read.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from agent.schema_prompt import system_blocks

from .envelope import AgentEnvelope, NodeTiming
from .executors import SqlExecutor

# The plan's build-against target (.agents/p4_agent.md "Model + cost"). The step-8
# sweep varies this across claude-opus-5 and claude-haiku-4-5; it is a constructor
# argument precisely so the sweep is a loop and not an edit.
DEFAULT_MODEL = "claude-sonnet-5"

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["answer", "clarify", "decline"],
            "description": "answer: write SQL. clarify: the question is genuinely "
                           "ambiguous. decline: the data cannot answer it.",
        },
        "summary": {
            "type": "string",
            "description": "For `answer`, one or two sentences on what the query "
                           "computes and any threshold or definition you chose. For "
                           "`clarify`, the specific question you need answered. For "
                           "`decline`, plainly what is missing from the data.",
        },
        "sql": {
            "type": ["string", "null"],
            "description": "The PostgreSQL SELECT statement. Null for clarify and decline.",
        },
    },
    "required": ["outcome", "summary", "sql"],
    "additionalProperties": False,
}


@dataclass
class _Reply:
    outcome: str
    summary: str
    sql: str | None
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


class ZeroShotBaseline:
    """Implements evals.harness.envelope.Agent."""

    def __init__(
        self,
        executor: SqlExecutor,
        *,
        model: str = DEFAULT_MODEL,
        effort: str | None = None,
        max_tokens: int = 4096,
        api_key: str | None = None,
    ) -> None:
        import anthropic

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        # A bare client also resolves an `ant auth login` profile, so an unset env
        # var is not by itself fatal -- let the SDK decide and fail on first call.
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self._executor = executor
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        suffix = f"-{self._effort}" if self._effort else ""
        return f"zero-shot-{self._model}{suffix}"

    def answer(self, question: str) -> AgentEnvelope:
        started = time.monotonic()
        timings: list[NodeTiming] = []

        draft_started = time.monotonic()
        try:
            reply = self._draft(question)
        except Exception as err:  # noqa: BLE001 - surfaced as a scored miss, see below
            # An API-level failure is scored as a miss rather than raised: one flaky
            # call should not abort a 25-case run. Genuine harness breakage (missing
            # key) fails every case identically and is obvious in the report.
            return AgentEnvelope(
                outcome="answer",
                summary="",
                error=f"model call failed: {type(err).__name__}: {err}",
                total_ms=(time.monotonic() - started) * 1000,
                model=self._model,
            )
        timings.append(NodeTiming("draft_sql", (time.monotonic() - draft_started) * 1000))

        envelope = AgentEnvelope(
            outcome=reply.outcome,  # type: ignore[arg-type]
            summary=reply.summary,
            sql=reply.sql,
            node_timings=timings,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            cache_creation_input_tokens=reply.cache_creation_input_tokens,
            cache_read_input_tokens=reply.cache_read_input_tokens,
            model=self._model,
        )

        if reply.outcome == "answer":
            if not reply.sql:
                envelope.error = "model chose `answer` but produced no SQL"
            else:
                exec_started = time.monotonic()
                outcome = self._executor.execute(reply.sql)
                timings.append(
                    NodeTiming("execute", (time.monotonic() - exec_started) * 1000)
                )
                envelope.tool_calls = 1
                if outcome.ok:
                    envelope.result = outcome.result
                else:
                    envelope.error = f"{outcome.status}: {outcome.error}"

        envelope.total_ms = (time.monotonic() - started) * 1000
        return envelope

    def _draft(self, question: str) -> _Reply:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system_blocks(cache=True),
            "messages": [{"role": "user", "content": question}],
            "output_config": {
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}
            },
        }
        if self._effort:
            kwargs["output_config"]["effort"] = self._effort

        response = self._client.messages.create(**kwargs)

        if response.stop_reason == "refusal":
            raise RuntimeError("model refused the request")

        text = next((b.text for b in response.content if b.type == "text"), "")
        payload = json.loads(text)
        usage = response.usage

        sql = payload.get("sql")
        return _Reply(
            outcome=payload["outcome"],
            summary=payload.get("summary", ""),
            sql=sql.strip() if isinstance(sql, str) and sql.strip() else None,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
