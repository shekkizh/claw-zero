"""Phase 9 — config validation + no cua imports."""

import sys

import pytest

from claw_zero.config import ClawZeroConfig


def test_defaults():
    c = ClawZeroConfig()
    assert c.model == "gpt-5.5"
    assert c.compaction_threshold == 0.8
    assert c.max_tool_result_chars == 16_000
    assert c.tick_seconds is None
    assert c.agent_id == "claw-zero"


def test_validation_rejects_bad_values():
    with pytest.raises(ValueError):
        ClawZeroConfig(compaction_threshold=0)
    with pytest.raises(ValueError):
        ClawZeroConfig(compaction_threshold=1.5)
    with pytest.raises(ValueError):
        ClawZeroConfig(max_tool_result_chars=0)
    with pytest.raises(ValueError):
        ClawZeroConfig(tick_seconds=0)
    with pytest.raises(ValueError):
        ClawZeroConfig(agent_id="  ")


def test_roster_defaults_empty_and_spawn_on():
    c = ClawZeroConfig()
    assert c.agents == []
    assert c.allow_spawn is True
    assert c.operator_id == "operator"


def test_roster_validation():
    # A clean roster is accepted.
    ClawZeroConfig(agent_id="lead", agents=["planner", "coder"])
    # Duplicate of the primary agent id.
    with pytest.raises(ValueError):
        ClawZeroConfig(agent_id="lead", agents=["lead"])
    # Duplicate within the roster.
    with pytest.raises(ValueError):
        ClawZeroConfig(agents=["a", "a"])
    # Empty id in the roster.
    with pytest.raises(ValueError):
        ClawZeroConfig(agents=["  "])


def test_operator_name_must_be_unique_and_nonempty():
    # Operator name collides with the primary agent.
    with pytest.raises(ValueError):
        ClawZeroConfig(agent_id="lead", operator_id="lead")
    # Operator name collides with a roster teammate.
    with pytest.raises(ValueError):
        ClawZeroConfig(agent_id="lead", operator_id="alex", agents=["alex"])
    # Empty operator name.
    with pytest.raises(ValueError):
        ClawZeroConfig(operator_id="  ")
    # A custom operator name that doesn't collide is fine.
    c = ClawZeroConfig(agent_id="lead", operator_id="alex", agents=["coder"])
    assert c.operator_id == "alex"


def test_no_effort_knob():
    # Reasoning is fixed in llm.py; config must not expose it.
    fields = ClawZeroConfig().__dataclass_fields__
    assert not any("effort" in f or "thinking" in f for f in fields)


def test_importing_config_pulls_no_heavy_or_cua_imports():
    # Importing config (and the package) must not import the LLM SDK or cua-*.
    import claw_zero.config  # noqa: F401

    assert "openai" not in sys.modules
    assert not any(name.startswith(("cua", "agent.", "computer")) for name in sys.modules), (
        "a cua/computer module was imported transitively"
    )
