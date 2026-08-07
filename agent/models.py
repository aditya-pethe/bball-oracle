"""Per-node model configuration (.agents/p4_agent.md "Model + cost").

Required, not optional: the step-8 sweep varies model AND effort independently
per node ("Haiku critic + Sonnet drafter is the cost-optimal frontier" is only
obtainable if the sweep can vary nodes independently), so this file is the one
place that has to change for that -- a config edit, not a code edit. Every node
defaults to `claude-sonnet-5`, the plan's build-against target. `classify` and
`critic` are the plan's flagged candidates for a cheaper model once the sweep
has numbers; `draft_sql` is the one node the plan says should stay on the
strongest model available.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MODEL = "claude-sonnet-5"

# output_config.effort. None omits the field entirely (model default applies).
Effort = str  # "low" | "medium" | "high" | "xhigh" | "max"


@dataclass(frozen=True)
class NodeModelConfig:
    model: str = DEFAULT_MODEL
    effort: Effort | None = None
    max_tokens: int = 4096


# One entry per node that calls the model. `execute` is deliberately absent: it
# is an HTTP call to /api/internal/query, not a model call. (There is no
# `validate` node at all -- see agent/graph.py for why.)
NODE_MODELS: dict[str, NodeModelConfig] = {
    "classify": NodeModelConfig(),
    "draft_sql": NodeModelConfig(),
    "critic": NodeModelConfig(),
    "summarize": NodeModelConfig(),
}


def get_model_config(node: str) -> NodeModelConfig:
    """Falls back to the default config for any node not listed above, so a
    new model-calling node works before anyone remembers to add an entry."""
    return NODE_MODELS.get(node, NodeModelConfig())
