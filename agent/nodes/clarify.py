"""clarify -- terminal node when classify judged the question genuinely ambiguous.

No model call here, by design. classify's structured output (agent/nodes/
classify.py) already produced `clarify_question` under the same OUTCOME_GUIDANCE
that governs what a good clarifying question looks like -- asking a second model
to rephrase what the first one already phrased correctly would add latency and
cost for no accuracy gain, and would risk the rephrase drifting from the
reasoning that justified clarifying in the first place. If a future multi-turn
flow needs to fold conversation history into the phrasing, that is a genuine
new capability and belongs in a real change, not a speculative call added here.
"""
from __future__ import annotations

from typing import Callable

from ..state import AgentState


def build() -> Callable[[AgentState], dict]:
    def clarify(state: AgentState) -> dict:
        return {
            "outcome": "clarify",
            "summary": state.get("clarify_question") or "",
        }

    return clarify
