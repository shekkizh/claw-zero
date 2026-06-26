"""ClawZeroConfig — the trimmed config for claw-zero.

Started from the ALE Claw ``config.py`` and stripped of every cua / GUI /
transport / delegation / web / thinking-level knob. What remains is exactly what
claw-zero uses. **There is no effort knob** — ``llm.py`` sends the same fixed
OpenAI reasoning setting on every call.

API keys are NEVER stored here. The OpenAI SDK reads ``OPENAI_API_KEY`` directly
from the process environment. This module imports nothing heavy (no ``openai``,
no ``cua-*``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .llm import DEFAULT_TOOL_OUTPUT_TOKENS


@dataclass
class ClawZeroConfig:
    """Per-run tunables for claw-zero."""

    model: str = "gpt-5.5"
    """Native OpenAI model id. Reasoning is fixed in ``llm.py``."""

    agents: list[str] = field(default_factory=list)
    """Extra teammate ids to launch at startup, beyond ``agent_id`` (the static
    roster). Empty → a single-agent run. Each shares ``model`` unless a runtime
    spawn overrides it. All run as equal peers on one bus (a flat mesh)."""

    allow_spawn: bool = True
    """Whether agents may bring new teammates online at runtime via the
    ``spawn_agent`` tool. The tool is only registered when this is True."""

    operator_id: str = "operator"
    """The human operator's participant name. The human is a named participant on
    the bus, addressed by this name like any agent — not a generic "human" role.
    Other participants reach them with this id (e.g. ``send_message(to=...)``)."""

    context_window_tokens: int | None = None
    """Override the resolved context window. None → resolve from the model
    (falls back to 200K when no local OpenAI metadata is known)."""

    auto_compact_token_limit: int | None = None
    """Prompt-token count at which compaction triggers. None → use the
    Codex-style default derived from the resolved context window."""

    tool_output_token_limit: int = DEFAULT_TOOL_OUTPUT_TOKENS
    """Approximate per-tool-output token cap. Converted to a conservative local
    character cap for shell/function outputs."""

    compaction_threshold: float | None = None
    """Compatibility alias. If set and ``auto_compact_token_limit`` is unset,
    compaction triggers at this fraction of the resolved context window."""

    max_tool_result_chars: int | None = None
    """Compatibility alias for the local character cap. Prefer
    ``tool_output_token_limit``."""

    tick_seconds: float | None = None
    """Self-tick interval in seconds. None = off (no background tick coroutine)."""

    agent_id: str = "claw-zero"
    """This agent's id; scopes its memory/transcript workspace and is the peer id
    other operators address."""

    base_dir: str | None = None
    """Root for state (memory + transcript). None → ``claw_zero_state``."""

    def __post_init__(self) -> None:
        if (
            self.auto_compact_token_limit is not None
            and self.auto_compact_token_limit <= 0
        ):
            raise ValueError(
                "auto_compact_token_limit must be positive or None, "
                f"got {self.auto_compact_token_limit!r}"
            )
        if self.tool_output_token_limit <= 0:
            raise ValueError(
                f"tool_output_token_limit must be positive, got {self.tool_output_token_limit!r}"
            )
        if (
            self.compaction_threshold is not None
            and not 0 < self.compaction_threshold <= 1
        ):
            raise ValueError(
                f"compaction_threshold must be in (0, 1], got {self.compaction_threshold!r}"
            )
        if self.max_tool_result_chars is not None and self.max_tool_result_chars <= 0:
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
        if not self.operator_id.strip():
            raise ValueError("operator_id must be a non-empty string")
        # Every participant name on the bus must be unique: the operator, the
        # primary agent, and each roster teammate. Names are how the bus routes,
        # so a collision would make a message ambiguous.
        if self.agent_id == self.operator_id:
            raise ValueError(
                f"agent_id and operator_id must differ (both {self.agent_id!r})"
            )
        seen = {self.agent_id, self.operator_id}
        for name in self.agents:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"agent ids must be non-empty strings, got {name!r}")
            if name in seen:
                raise ValueError(
                    f"duplicate participant name {name!r} (clashes with operator or another agent)"
                )
            seen.add(name)
