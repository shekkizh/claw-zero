"""Phase 9 — config validation + no cua imports."""

import sys

import pytest

from claw_zero.config import ClawZeroConfig


def test_defaults():
    c = ClawZeroConfig()
    assert c.model == "openai/gpt-5.5"
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


def test_no_effort_knob():
    # Effort is always max via the thinking layer — config must not expose it.
    fields = ClawZeroConfig().__dataclass_fields__
    assert not any("effort" in f or "thinking" in f for f in fields)


def test_importing_config_pulls_no_cua():
    # Importing config (and the package) must not import any cua-* module.
    import claw_zero.config  # noqa: F401

    assert not any(name.startswith(("cua", "agent.", "computer")) for name in sys.modules), (
        "a cua/computer module was imported transitively"
    )
