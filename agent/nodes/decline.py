"""decline -- terminal node when classify judged the data cannot answer at all.

No model call here, for the same reason as clarify.py: classify's structured
output already produced `decline_reason` under OUTCOME_GUIDANCE, which already
instructs it to state plainly what's missing and to optionally offer a
statistical proxy, clearly labeled as a proxy. A second call here would just
be asking a model to restate what the first one already said -- an added round
trip for no accuracy gain, and a place a proxy offered by classify could get
silently dropped or reworded by a model that never saw the reasoning behind it.
"""
from __future__ import annotations

from typing import Callable

from ..state import AgentState


def build() -> Callable[[AgentState], dict]:
    def decline(state: AgentState) -> dict:
        return {
            "outcome": "decline",
            "summary": state.get("decline_reason") or "",
        }

    return decline
