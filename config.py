"""ClawZeroConfig — the trimmed config for claw-zero.

Started from the ALE Claw ``config.py`` and stripped of every cua / GUI /
transport / delegation / web / thinking-level knob. What remains is exactly what
claw-zero uses. **There is no effort knob** — effort is always max via the
thinking layer in ``llm.py`` (see ``MAX_EFFORT``).

API keys are NEVER stored here. litellm reads ``OPENAI_API_KEY`` /
``ANTHROPIC_API_KEY`` / ``OPENROUTER_API_KEY`` etc. directly from the process
environment. This module imports nothing heavy (no ``litellm``, no ``cua-*``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClawZeroConfig:
    """Per-run tunables for claw-zero."""

    model: str = "openai/gpt-5.5"
    """LiteLLM-format model id (provider/name). Effort is always max."""

    context_window_tokens: int | None = None
    """Override the resolved context window. None → resolve from the model
    (falls back to 200K when litellm can't resolve it)."""

    compaction_threshold: float = 0.8
    """Fraction of the context window at which compaction (and the
    pre-compaction memory flush) trigger."""

    max_tool_result_chars: int = 16_000
    """Per-tool-result content cap (also the bash per-stream truncation cap)."""

    tick_seconds: float | None = None
    """Self-tick interval in seconds. None = off (no background tick coroutine)."""

    agent_id: str = "claw-zero"
    """This agent's id; scopes its memory/transcript workspace and is the peer id
    other operators address."""

    base_dir: str | None = None
    """Root for state (memory + transcript). None → ``claw_zero_state``."""

    def __post_init__(self) -> None:
        if not 0 < self.compaction_threshold <= 1:
            raise ValueError(
                f"compaction_threshold must be in (0, 1], got {self.compaction_threshold!r}"
            )
        if self.max_tool_result_chars <= 0:
            raise ValueError(
                f"max_tool_result_chars must be positive, got {self.max_tool_result_chars!r}"
            )
        if self.tick_seconds is not None and self.tick_seconds <= 0:
            raise ValueError(f"tick_seconds must be positive or None, got {self.tick_seconds!r}")
        if self.context_window_tokens is not None and self.context_window_tokens <= 0:
            raise ValueError(
                f"context_window_tokens must be positive or None, got {self.context_window_tokens!r}"
            )
        if not self.agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
