"""The conversation-context contract (.agents/p5_conversational_context.md).

Phase 5 makes the agent tab conversational. The unit of context is one persisted
conversation, and this module is the single definition of what part of that
conversation reaches the model.

Three things live here and nothing else:

1. **The shape.** `ConversationContext` / `ConversationTurn` -- the Pydantic half
   of a contract whose other half is `web/lib/conversation-context.ts`. Next.js
   is the authority for ownership and persistence; it reads a bounded window from
   `app.conversation_message` and sends it as this shape. The service never
   fetches a conversation itself and still holds no database credential.

2. **The bounds.** They are enforced in the model's own validator rather than at
   the call site, so *every* construction path is bounded -- a payload arriving
   over HTTP from a buggy (or compromised) producer is clamped exactly like one
   built locally by the eval harness. Bounds that are only applied by the
   producer are not bounds.

3. **The projection.** `context_messages()` renders a node-appropriate view.
   Passing the same transcript to every node is the failure mode this is written
   against: `classify` needs prior questions and outcomes, `draft_sql` needs the
   last successful SQL to transform, and `critic`/`summarize` need only enough of
   the last exchange to know what "that" referred to.

What deliberately never enters the context: node timings, token counts,
transport errors, and full result sets. Only what helps resolve intent.

Prior turns are rendered as real user/assistant messages (built with LangChain's
provider-neutral primitives) rather than as one flattened transcript string.
They sit after the cached system prefix (agent/schema_prompt.py), so the stable
part of the prompt stays cacheable while the volatile part varies per turn.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, model_validator

from .envelope import AgentEnvelope, ExecResult
from .prompts import render

Role = Literal["user", "assistant"]
Outcome = Literal["answer", "clarify", "decline"]

# Initial bounds, to be validated against multi-turn eval latency/token
# measurements -- not permanent product constants
# (.agents/p5_conversational_context.md "Recommended initial bounds").
#
# 16 messages is roughly eight completed exchanges. Raised from 8 on 2026-08-11:
# eight was measured too tight on a real thread, where one answered question
# followed by seven clarifications pushed the only answer out of the window. The
# measured cost of carrying context was ~+30% input tokens per exchange with the
# cache still serving over 90%, so the headroom is affordable.
#
# 5 preview rows is enough for the drafter to see what the previous answer looked
# like without shipping a result set. The byte ceiling is the backstop the other
# two cannot provide: a single turn can carry a long SQL string or wide text
# cells, and "16 messages" says nothing about how big a message is. It scales
# with the message cap so that raising one does not silently leave the other
# binding first.
MAX_CONTEXT_MESSAGES = 16
MAX_PREVIEW_ROWS = 5
MAX_SERIALIZED_BYTES = 16000


class ResultShape(BaseModel):
    """What a previous result looked like, without the result."""

    model_config = ConfigDict(frozen=True)

    columns: list[str] = []
    row_count: int = 0
    truncated: bool = False


class ConversationTurn(BaseModel):
    """One persisted turn, projected down to what helps resolve intent.

    `text` is the user's question or the assistant's summary. A turn that failed
    carries the failure in `text` and drops `sql` entirely -- SQL that errored is
    not a base a follow-up should be transformed from, and leaving it in invites
    exactly that.
    """

    role: Role
    text: str = ""
    outcome: Optional[Outcome] = None
    sql: Optional[str] = None
    result_shape: Optional[ResultShape] = None
    result_preview: Optional[list[list[Any]]] = None

    @model_validator(mode="after")
    def _bound_preview(self) -> "ConversationTurn":
        if self.result_preview is not None and len(self.result_preview) > MAX_PREVIEW_ROWS:
            # Trim the preview, never `result_shape.row_count`: how many rows the
            # query actually returned is part of the answer's meaning, and a
            # rewritten count would make a 500-row result read as a 5-row one.
            self.result_preview = self.result_preview[:MAX_PREVIEW_ROWS]
        return self

    @property
    def has_reusable_sql(self) -> bool:
        return bool(self.sql) and self.outcome == "answer"


@dataclass(frozen=True)
class PendingClarification:
    original_question: str
    clarify_question: str


class ConversationContext(BaseModel):
    """A bounded window onto one conversation, oldest turn first."""

    turns: list[ConversationTurn] = []

    @model_validator(mode="after")
    def _bound_turns(self) -> "ConversationContext":
        self.turns = _fit_bytes(_clamp_count(self.turns))
        return self

    @property
    def pending_clarification(self) -> PendingClarification | None:
        """An unresolved clarification, or None.

        Message order is the source of truth -- there is no mutable "resolved"
        column (.agents/p5_conversational_context.md). The latest assistant turn
        having asked for clarification IS the pending state; any later assistant
        answer or decline is what closes it, because it would be the latest turn
        instead.
        """
        index = _last_assistant_index(self.turns)
        if index is None or self.turns[index].outcome != "clarify":
            return None
        question = _preceding_user_text(self.turns, index)
        if question is None:
            return None
        return PendingClarification(
            original_question=question,
            clarify_question=self.turns[index].text,
        )

    @property
    def has_exchange(self) -> bool:
        """Whether anything here is actually a conversation.

        A window of user turns and no assistant turn is not history, it is a list
        of questions nobody answered -- which is exactly what the context loader
        produces when the previous turn is still an unfinished placeholder.
        """
        return any(turn.role == "assistant" for turn in self.turns)

    @property
    def usable_turns(self) -> list[ConversationTurn]:
        """The window with trailing unanswered questions removed.

        A user turn with no assistant turn after it never got an answer -- it is
        an interrupted turn, or one still in flight. Sending it as context puts
        the same question in front of the model twice (`_normalize` merges it into
        the current task, since both are user turns) wrapped in an instruction to
        resolve references against it. Measured in production on 2026-08-10, where
        a turn stuck at `{pending: true}` degraded every later turn in its thread
        (.agents/p5_regression_report.md).
        """
        turns = list(self.turns)
        while turns and turns[-1].role == "user":
            turns.pop()
        return turns

    @property
    def last_reusable_turn(self) -> ConversationTurn | None:
        """The most recent successfully answered turn, whose SQL a follow-up can
        transform. Failed and abstained turns are skipped."""
        for turn in reversed(self.turns):
            if turn.has_reusable_sql:
                return turn
        return None


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def _last_assistant_index(turns: list[ConversationTurn]) -> int | None:
    for index in range(len(turns) - 1, -1, -1):
        if turns[index].role == "assistant":
            return index
    return None


def _preceding_user_text(turns: list[ConversationTurn], index: int) -> str | None:
    for i in range(index - 1, -1, -1):
        if turns[i].role == "user":
            return turns[i].text
    return None


def _pending_pair(turns: list[ConversationTurn]) -> tuple[int, int] | None:
    """Indices of (original question, clarifying question) for an unresolved
    clarification, so the count bound can pin them."""
    index = _last_assistant_index(turns)
    if index is None or turns[index].outcome != "clarify":
        return None
    for i in range(index - 1, -1, -1):
        if turns[i].role == "user":
            return (i, index)
    return None


def _last_answered_pair(turns: list[ConversationTurn]) -> tuple[int, int] | None:
    """Indices of (question, answer) for the most recent successfully answered
    exchange -- the thing a follow-up actually needs and the drafter transforms."""
    for index in range(len(turns) - 1, -1, -1):
        if not turns[index].has_reusable_sql:
            continue
        for j in range(index - 1, -1, -1):
            if turns[j].role == "user":
                return (j, index)
        return None
    return None


def _protected_indices(turns: list[ConversationTurn]) -> set[int]:
    """Turns that survive trimming regardless of age.

    Two of them, and both are exchanges rather than single turns, because half an
    exchange resumes nothing:

    - the most recent ANSWERED exchange. Trimming used to count messages, which
      valued a content-free "I need clarification" turn exactly as highly as an
      answer carrying SQL and results. Measured on a real thread on 2026-08-11:
      one answered question followed by seven clarifications evicted the only
      answer, leaving the model eight messages of its own requests for
      clarification and nothing to anchor a follow-up to.
    - an unresolved clarification, for the reason it always was: without the
      original question, "you asked the user to clarify" is not a resumable task.
    """
    protected: set[int] = set()
    pending = _pending_pair(turns)
    if pending is not None:
        protected.update(pending)
    answered = _last_answered_pair(turns)
    if answered is not None:
        protected.update(answered)
    return protected


def _clamp_count(turns: list[ConversationTurn]) -> list[ConversationTurn]:
    if len(turns) <= MAX_CONTEXT_MESSAGES:
        return turns

    # Protected turns first, then fill the remaining budget from the most recent
    # end backwards. Recency still governs everything that is not protected --
    # what "that" or "him" refers to is almost always the previous turn.
    keep = set(_protected_indices(turns))
    budget = MAX_CONTEXT_MESSAGES - len(keep)
    for index in range(len(turns) - 1, -1, -1):
        if budget <= 0:
            break
        if index in keep:
            continue
        keep.add(index)
        budget -= 1

    # Sorted, because the kept turns are spliced from two regions of the thread
    # and a conversation rendered out of order is worse than a shorter one.
    return [turns[index] for index in sorted(keep)]


def _size(turns: list[ConversationTurn]) -> int:
    return len(json.dumps([t.model_dump() for t in turns], default=str))


def _halve_strings(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 1:
        return value[: len(value) // 2]
    return value


def _shrink(turn: ConversationTurn) -> ConversationTurn:
    return turn.model_copy(
        update={
            "text": _halve_strings(turn.text),
            "sql": _halve_strings(turn.sql) if turn.sql else turn.sql,
            "result_preview": (
                [[_halve_strings(cell) for cell in row] for row in turn.result_preview]
                if turn.result_preview
                else turn.result_preview
            ),
        }
    )


def _fit_bytes(turns: list[ConversationTurn]) -> list[ConversationTurn]:
    """The hard serialized-size ceiling, enforced before the service call.

    Oldest turns go first, and protected ones go last -- otherwise this stage
    would silently undo `_clamp_count`'s work the moment a few turns carried long
    SQL, dropping the answered exchange it had just gone out of its way to keep.
    The protection is recomputed each pass because indices shift as turns leave.

    The last turn is never dropped -- it is the one the current question is most
    likely to be a follow-up to, and an empty context reads to the model as "no
    history", which is a different (and wrong) statement than "history was too
    big to send".
    """
    turns = list(turns)
    while len(turns) > 1 and _size(turns) > MAX_SERIALIZED_BYTES:
        protected = _protected_indices(turns)
        victim = next((i for i in range(len(turns)) if i not in protected), 0)
        turns.pop(victim)

    guard = 0
    while turns and _size(turns) > MAX_SERIALIZED_BYTES and guard < 64:
        turns = [_shrink(t) for t in turns]
        guard += 1
    return turns


# ---------------------------------------------------------------------------
# Building context (used by the eval harness; production builds it in TypeScript)
# ---------------------------------------------------------------------------


def append_user_turn(
    context: ConversationContext | None, question: str
) -> ConversationContext:
    turns = list(context.turns) if context else []
    return ConversationContext(turns=[*turns, ConversationTurn(role="user", text=question)])


def append_assistant_turn(
    context: ConversationContext | None, envelope: AgentEnvelope
) -> ConversationContext:
    """Projects a finished envelope down to a context turn.

    The same projection `web/lib/conversation-context.ts` performs against a
    persisted row, so the eval harness exercises the production contract rather
    than an eval-only prompt path.
    """
    turns = list(context.turns) if context else []
    return ConversationContext(turns=[*turns, turn_from_envelope(envelope)])


def turn_from_envelope(envelope: AgentEnvelope) -> ConversationTurn:
    failed = envelope.error is not None
    text = envelope.summary or ""
    if failed:
        text = f"(this turn failed: {envelope.error})" if not text else f"{text} (failed: {envelope.error})"

    return ConversationTurn(
        role="assistant",
        text=text,
        outcome=envelope.outcome,
        # A query that errored is not a transformation base for a follow-up.
        sql=None if failed else envelope.sql,
        result_shape=_shape(envelope.result),
        result_preview=_preview(envelope.result),
    )


def _shape(result: ExecResult | None) -> ResultShape | None:
    if result is None:
        return None
    return ResultShape(
        columns=list(result.columns), row_count=result.row_count, truncated=result.truncated
    )


def _preview(result: ExecResult | None) -> list[list[Any]] | None:
    if result is None:
        return None
    return [list(row) for row in result.rows[:MAX_PREVIEW_ROWS]]


# ---------------------------------------------------------------------------
# Clarification continuation
# ---------------------------------------------------------------------------


def resolve_question(question: str, context: ConversationContext | None) -> str:
    """The task the graph actually works on this turn.

    With no pending clarification this is the question as asked. With one, it is
    the original question, the clarification the agent asked for, and the user's
    answer folded into a single task -- so the existing classifier still runs and
    can still answer, narrow, or decline. No special execution path bypasses
    classification (.agents/p5_conversational_context.md "Clarification
    continuation").
    """
    pending = context.pending_clarification if context else None
    if pending is None:
        return question
    return render(
        "clarification_continuation",
        original_question=pending.original_question,
        clarify_question=pending.clarify_question,
        answer=question,
    )


# ---------------------------------------------------------------------------
# Node-specific rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rendering:
    """How much of the context one node gets.

    `recent_turns=None` means the whole bounded window; a number means only that
    many trailing turns. `last_sql` adds the most recent successful query and its
    result shape to the turn that produced it.
    """

    recent_turns: int | None
    last_sql: bool


_RENDERINGS: dict[str, _Rendering] = {
    # Needs the arc of the conversation to tell a follow-up from a new topic,
    # and to see an unresolved clarification. Never needs SQL.
    "classify": _Rendering(recent_turns=None, last_sql=False),
    # The only node that transforms a previous query, so the only one that gets
    # one to transform.
    "draft_sql": _Rendering(recent_turns=None, last_sql=True),
    # Judges this turn's SQL against the resolved question; it needs to know what
    # "that" referred to, not the whole thread, and re-reading prior SQL invites
    # rejecting a correct query for not matching an older one.
    "critic": _Rendering(recent_turns=2, last_sql=False),
    # Carries forward assumptions stated in the previous answer. Prior results
    # are not part of narrating this one.
    "summarize": _Rendering(recent_turns=2, last_sql=False),
}


def _describe_result(turn: ConversationTurn) -> str:
    shape = turn.result_shape
    if shape is None:
        return ""
    lines = [f"Result: columns={shape.columns!r}, {shape.row_count} row(s) total"]
    if turn.result_preview:
        lines.append(f"first {len(turn.result_preview)} row(s): {turn.result_preview!r}")
    return "\n".join(lines)


def _assistant_content(turn: ConversationTurn, *, with_sql: bool) -> str:
    parts = [f"[{turn.outcome or 'answer'}] {turn.text}".strip()]
    if with_sql and turn.sql:
        parts.append(f"SQL:\n{turn.sql}")
        described = _describe_result(turn)
        if described:
            parts.append(described)
    return "\n\n".join(p for p in parts if p)


def _lc_messages(context: ConversationContext, rendering: _Rendering) -> list[BaseMessage]:
    turns = context.usable_turns
    if rendering.recent_turns is not None:
        turns = turns[-rendering.recent_turns :]

    sql_turn = context.last_reusable_turn if rendering.last_sql else None

    messages: list[BaseMessage] = []
    for turn in turns:
        if turn.role == "user":
            messages.append(HumanMessage(content=turn.text))
        else:
            messages.append(
                AIMessage(content=_assistant_content(turn, with_sql=turn is sql_turn))
            )
    return messages


def _normalize(messages: list[dict]) -> list[dict]:
    """Anthropic requires the first message to be a user turn and does not want
    two consecutive turns from the same role. A pinned clarification pair spliced
    onto a recent window can produce both, so fix it here rather than at four
    call sites."""
    out: list[dict] = []
    for message in messages:
        if not out and message["role"] != "user":
            continue
        if out and out[-1]["role"] == message["role"]:
            out[-1] = {**out[-1], "content": f"{out[-1]['content']}\n\n{message['content']}"}
            continue
        out.append(dict(message))
    return out


def _to_dict(message: BaseMessage) -> dict:
    """LangChain message -> the role/content dict `ModelClient.complete` speaks."""
    return {"role": "user" if isinstance(message, HumanMessage) else "assistant",
            "content": message.content}


def context_messages(context: ConversationContext | None, node: str) -> list[dict]:
    """Prior turns as messages, rendered for `node`. Empty when there is no
    context, or when the node does not call the model."""
    rendering = _RENDERINGS.get(node)
    if context is None or not context.has_exchange or rendering is None:
        return []
    return _normalize([_to_dict(m) for m in _lc_messages(context, rendering)])


def build_messages(
    context: ConversationContext | None, node: str, content: str
) -> list[dict]:
    """The full `messages` argument for a node: prior turns, then this turn's task."""
    return _normalize([*context_messages(context, node), {"role": "user", "content": content}])


# ---------------------------------------------------------------------------
# What nodes call
# ---------------------------------------------------------------------------

def context_of(state: Mapping[str, Any]) -> ConversationContext | None:
    return state.get("conversation_context")


def task_question(state: Mapping[str, Any]) -> str:
    """The question the graph is actually working on this turn.

    Seeded once by `agent.state.new_state` -- a node must never re-derive it, or a
    clarification continuation could mean one thing to the classifier and another
    to the drafter.
    """
    return state.get("resolved_question") or state["question"]


def node_messages(state: Mapping[str, Any], node: str, content: str) -> list[dict]:
    return build_messages(context_of(state), node, content)
