"""llm.py — claw-zero's single LLM entry point, driven through litellm.

This folds together the reusable, cua-free pieces of the ALE Claw harness model
layer into one self-contained module:

  - **thinking/effort** (ported from ``harness/model/thinking.py``): map a
    ``ThinkLevel`` to provider-specific reasoning kwargs. claw-zero always calls
    at **max effort** — there is no per-site effort knob. Every call site (main
    loop, compaction, memory flush) passes ``MAX_EFFORT``.
  - **model resolution** (trimmed from ``harness/model/model_config.py``):
    provider inference + context-window lookup. The computer-use format fields
    (tool schema type, screenshot type, action format, adapter target) are
    dropped — claw-zero has no GUI.
  - **cache policy** (ported from ``harness/model/cache_policy.py``): sliding
    ``cache_control`` breakpoints for Anthropic-family models, splitting the
    system prompt at a byte-stable boundary so volatile content sits below the
    cached prefix.
  - **the call** (ported from ``harness/model/helper_runtime.py``): one
    ``litellm.acompletion`` round-trip returning a normalized result.

``call()`` is the only function the rest of claw-zero uses to reach a model.
It keeps LiteLLM's model-string format, so Anthropic / OpenAI / Bedrock / Vertex
all work without any additional SDK. API keys are read from the environment by
litellm — never from config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ===========================================================================
# Thinking / effort  (ported from harness/model/thinking.py)
# ===========================================================================

class ThinkLevel(str, Enum):
    """Reasoning depth. claw-zero only ever uses ``XHIGH`` (see ``MAX_EFFORT``)."""

    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    ADAPTIVE = "adaptive"


MAX_EFFORT: ThinkLevel = ThinkLevel.XHIGH
"""The single effort level claw-zero passes everywhere. Thinking is always max."""


# Anthropic thinking-budget mapping (harness extra-params.ts Anthropic section).
_ANTHROPIC_BUDGETS: dict[ThinkLevel, int] = {
    ThinkLevel.MINIMAL: 2000,
    ThinkLevel.LOW: 5000,
    ThinkLevel.MEDIUM: 10000,
    ThinkLevel.HIGH: 16000,
    ThinkLevel.XHIGH: 25000,
    ThinkLevel.ADAPTIVE: 10000,
}

# OpenAI only supports low/medium/high — collapse our levels.
_OPENAI_EFFORT: dict[ThinkLevel, str] = {
    ThinkLevel.MINIMAL: "low",
    ThinkLevel.LOW: "low",
    ThinkLevel.MEDIUM: "medium",
    ThinkLevel.HIGH: "high",
    ThinkLevel.XHIGH: "high",
    ThinkLevel.ADAPTIVE: "medium",
}

_GEMINI_LEVELS: dict[ThinkLevel, str] = {
    ThinkLevel.MINIMAL: "MINIMAL",
    ThinkLevel.LOW: "LOW",
    ThinkLevel.MEDIUM: "MEDIUM",
    ThinkLevel.HIGH: "HIGH",
    ThinkLevel.XHIGH: "HIGH",
    ThinkLevel.ADAPTIVE: "MEDIUM",
}


def resolve_thinking_params(level: ThinkLevel, model: str) -> dict[str, Any]:
    """Map a ``ThinkLevel`` to provider-specific reasoning kwargs for ``acompletion``.

    Uses the chat-transport mapping (claw-zero drives everything through
    ``litellm.acompletion``): OpenAI gets ``reasoning_effort``, Anthropic gets a
    ``thinking`` budget, Gemini gets ``thinking_level``, OpenRouter gets a unified
    ``reasoning`` block. Returns ``{}`` for ``OFF`` (never used here).
    """
    if level == ThinkLevel.OFF:
        return {}

    m = model.lower()
    if m.startswith("openrouter/"):
        return {"reasoning": {"effort": _OPENAI_EFFORT.get(level, "medium")}}
    if "anthropic/" in m or "claude" in m:
        return {"thinking": {"type": "enabled", "budget_tokens": _ANTHROPIC_BUDGETS.get(level, 10000)}}
    if _is_openai_model(m):
        return {"reasoning_effort": _OPENAI_EFFORT.get(level, "medium")}
    if "gemini" in m or "google" in m or "vertex" in m:
        return {"thinking_level": _GEMINI_LEVELS.get(level, "MEDIUM")}
    return {"reasoning_effort": level.value}


def max_thinking_params(model: str) -> dict[str, Any]:
    """Reasoning kwargs at max effort for ``model``. The canonical claw-zero call."""
    return resolve_thinking_params(MAX_EFFORT, model)


def _is_openai_model(model: str) -> bool:
    m = model.lower()
    return "openai" in m or "gpt" in m or m.startswith(("o1", "o3", "o4"))


# ===========================================================================
# Model resolution  (trimmed from harness/model/model_config.py)
# ===========================================================================

DEFAULT_CONTEXT_TOKENS = 200_000
"""Fallback context window when litellm can't resolve the model."""


@dataclass(frozen=True)
class ResolvedModel:
    """Capability-light resolved metadata for a litellm model string."""

    model: str
    model_id: str
    provider: str
    context_window: int


def _infer_provider(model: str) -> str:
    m = model.lower()
    if m.startswith("openrouter/"):
        # provider is the segment after openrouter/
        rest = m.split("/", 1)[1]
        if rest.startswith("anthropic") or "claude" in rest:
            return "anthropic"
        if "openai" in rest or "gpt" in rest:
            return "openai"
        if "gemini" in rest or "google" in rest:
            return "google"
        return "openrouter"
    if m.startswith("anthropic/") or "claude" in m:
        return "anthropic"
    if _is_openai_model(m):
        return "openai"
    if "gemini" in m or "google" in m:
        return "google"
    if "vertex" in m:
        return "vertex"
    return "unknown"


def _lookup_context_window(model: str) -> int | None:
    """Best-effort context-window lookup via litellm's model registry."""
    candidates = [model]
    if "/" in model:
        candidates.append(model.split("/", 1)[1])
    for candidate in candidates:
        try:
            import litellm

            info = litellm.get_model_info(candidate)
            max_input = info.get("max_input_tokens")
            if max_input and max_input > 0:
                return int(max_input)
        except Exception:
            continue
    return None


def resolve_model(model: str | ResolvedModel) -> ResolvedModel:
    """Resolve a litellm model string into ``ResolvedModel`` metadata."""
    if isinstance(model, ResolvedModel):
        return model
    return ResolvedModel(
        model=model,
        model_id=model.split("/", 1)[-1] if "/" in model else model,
        provider=_infer_provider(model),
        context_window=_lookup_context_window(model) or DEFAULT_CONTEXT_TOKENS,
    )


def resolve_context_window(model: str) -> int:
    """Context window in tokens for ``model`` (fallback ``DEFAULT_CONTEXT_TOKENS``)."""
    return resolve_model(model).context_window


# ===========================================================================
# Cache policy  (ported from harness/model/cache_policy.py)
# ===========================================================================

CACHE_BOUNDARY = "<!-- CLAW_ZERO_CACHE_BOUNDARY -->"
"""Marker the prompt builder inserts between the byte-stable prefix and the
volatile suffix. Content above is cached; content below is not. (Anthropic only.)
"""

_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


def supports_anthropic_cache(model: str | None) -> bool:
    """True iff Anthropic ``cache_control`` markers are honored for ``model``."""
    if not model:
        return False
    m = model.lower()
    return (
        m.startswith("anthropic/")
        or m.startswith("openrouter/anthropic/")
        or m.startswith("vertex_ai/claude")
        or m.startswith("vertex_ai/anthropic")
        or m.startswith("bedrock/anthropic")
        or m.startswith("claude-")
    )


def apply_cache_markers(messages: list[dict[str, Any]] | None, model: str | None) -> None:
    """Apply the sliding-breakpoint ``cache_control`` pattern in place.

    1. Strip any pre-existing message-level markers (clean slate).
    2. If ``model`` isn't Anthropic-family, also strip block-level markers and
       return (no caching available; the marker is consumed by the system split
       regardless so the boundary text never reaches the model).
    3. Otherwise mark the system prompt (splitting at ``CACHE_BOUNDARY`` when
       present) and the trailing message — a sliding breakpoint so the cached
       prefix grows turn by turn.
    """
    if not messages:
        return

    for msg in messages:
        msg.pop("cache_control", None)

    anthropic = supports_anthropic_cache(model or "")
    if not anthropic:
        # Still consume the boundary marker from the system text so it is never
        # sent to a non-Anthropic model, but apply no cache_control.
        _consume_boundary_only(messages[0])
        for msg in messages:
            _strip_block_cache_control(msg)
        return

    _apply_system_cache(messages[0])
    last = messages[-1]
    if last is not messages[0]:
        last["cache_control"] = dict(_EPHEMERAL)


def _consume_boundary_only(msg: dict[str, Any]) -> None:
    content = msg.get("content")
    if isinstance(content, str) and CACHE_BOUNDARY in content:
        stable, dynamic = content.split(CACHE_BOUNDARY, 1)
        msg["content"] = (stable.rstrip() + "\n\n" + dynamic.lstrip()).strip()


def _apply_system_cache(msg: dict[str, Any]) -> None:
    """Mark the system prompt for caching, splitting at the boundary if present."""
    content = msg.get("content")
    if isinstance(content, str):
        if CACHE_BOUNDARY in content:
            stable, dynamic = content.split(CACHE_BOUNDARY, 1)
            blocks: list[dict[str, Any]] = []
            stable = stable.rstrip()
            if stable:
                blocks.append({"type": "text", "text": stable, "cache_control": dict(_EPHEMERAL)})
            dynamic = dynamic.lstrip()
            if dynamic:
                blocks.append({"type": "text", "text": dynamic})
            msg["content"] = blocks
        else:
            msg["cache_control"] = dict(_EPHEMERAL)
        return
    # Unknown shape — fall back to a message-level marker.
    msg["cache_control"] = dict(_EPHEMERAL)


def _strip_block_cache_control(msg: dict[str, Any]) -> None:
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                block.pop("cache_control", None)


# ===========================================================================
# The call  (ported from harness/model/helper_runtime.py)
# ===========================================================================

@dataclass
class ToolCall:
    """One normalized tool call from the model."""

    id: str
    name: str
    arguments: str  # raw JSON string as the model emitted it


@dataclass
class LLMResult:
    """Normalized result of a single model call."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


async def call(
    model: str,
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8192,
    temperature: float = 1.0,
    timeout: int | None = None,
    effort: ThinkLevel = MAX_EFFORT,
    cache: bool = True,
) -> LLMResult:
    """Do one tool-calling model call via ``litellm.acompletion`` and normalize it.

    Args:
        model: litellm model string (e.g. ``openai/gpt-5.5``).
        messages: OpenAI chat-shaped messages. If ``system`` is given it is
            prepended as ``messages[0]`` (so cache markers land on it).
        system: Optional system prompt text, prepended as a system message.
        tools: OpenAI tool specs (``{"type": "function", "function": {...}}``).
        max_tokens: Output token cap.
        temperature: Sampling temperature (1.0 — reasoning models require it).
        timeout: Per-call timeout in seconds, or ``None``.
        effort: Reasoning effort. Defaults to ``MAX_EFFORT`` and should stay
            there — claw-zero is always-max.
        cache: Apply Anthropic cache markers (no-op for other providers).

    Returns:
        ``LLMResult`` with ``text``, ``tool_calls``, ``finish_reason``, ``usage``.
    """
    import litellm

    full_messages: list[dict[str, Any]] = list(messages)
    if system is not None:
        full_messages = [{"role": "system", "content": system}, *full_messages]

    if cache:
        apply_cache_markers(full_messages, model)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": full_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Reasoning models (and provider param quirks) — let litellm drop kwargs
        # a given provider doesn't accept rather than erroring.
        "drop_params": True,
        **resolve_thinking_params(effort, model),
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    if tools:
        kwargs["tools"] = tools

    response = await litellm.acompletion(**kwargs)
    return _normalize(response)


def _normalize(response: Any) -> LLMResult:
    """Convert a litellm chat-completion response into ``LLMResult``."""
    choice = response.choices[0]
    message = choice.message

    tool_calls: list[ToolCall] = []
    for tc in (getattr(message, "tool_calls", None) or []):
        fn = getattr(tc, "function", None)
        tool_calls.append(
            ToolCall(
                id=getattr(tc, "id", "") or "",
                name=getattr(fn, "name", "") or "",
                arguments=getattr(fn, "arguments", "") or "{}",
            )
        )

    usage_obj = getattr(response, "usage", None)
    usage: dict[str, int] = {}
    if usage_obj is not None:
        for src, dst in (
            ("prompt_tokens", "input_tokens"),
            ("completion_tokens", "output_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            val = getattr(usage_obj, src, None)
            if isinstance(val, (int, float)) and val > 0:
                usage[dst] = int(val)

    return LLMResult(
        text=(getattr(message, "content", None) or "").strip(),
        tool_calls=tool_calls,
        finish_reason=getattr(choice, "finish_reason", "") or "",
        usage=usage,
    )
