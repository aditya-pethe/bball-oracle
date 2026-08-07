"""Per-node model config: the step-8 sweep depends on this being a loop, not
an edit -- these tests just pin the default and the override mechanics."""
from __future__ import annotations

from agent.models import DEFAULT_MODEL, NODE_MODELS, NodeModelConfig, get_model_config


def test_every_model_calling_node_defaults_to_the_build_target():
    for node in ("classify", "draft_sql", "critic", "summarize"):
        config = get_model_config(node)
        assert config.model == DEFAULT_MODEL == "claude-sonnet-5"
        assert config.effort is None


def test_unknown_node_falls_back_to_default_config():
    config = get_model_config("some_future_node")
    assert config == NodeModelConfig()


def test_overriding_one_node_does_not_affect_others():
    original = NODE_MODELS["critic"]
    try:
        NODE_MODELS["critic"] = NodeModelConfig(model="claude-haiku-4-5", effort="low")
        assert get_model_config("critic").model == "claude-haiku-4-5"
        assert get_model_config("draft_sql").model == DEFAULT_MODEL
    finally:
        NODE_MODELS["critic"] = original
