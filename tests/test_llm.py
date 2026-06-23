"""Phase 2 — unit tests for the pure pieces of llm.py (no network).

The live-model acceptance (a real openai/gpt-5.5 call) is exercised separately
in the Phase 2 verification step, not here, so the suite stays offline.
"""

from claw_zero import llm
from claw_zero.llm import CACHE_BOUNDARY, MAX_EFFORT, ThinkLevel


def test_max_effort_is_xhigh():
    assert MAX_EFFORT is ThinkLevel.XHIGH


def test_thinking_params_per_provider_at_max():
    # OpenAI → reasoning_effort (xhigh collapses to high)
    assert llm.max_thinking_params("openai/gpt-5.5") == {"reasoning_effort": "high"}
    # Anthropic → thinking budget
    p = llm.max_thinking_params("anthropic/claude-opus-4-8")
    assert p["thinking"]["type"] == "enabled" and p["thinking"]["budget_tokens"] == 25000
    # OpenRouter → unified reasoning block
    assert llm.max_thinking_params("openrouter/openai/gpt-5.5") == {"reasoning": {"effort": "high"}}
    # Gemini → thinking_level
    assert llm.max_thinking_params("gemini/gemini-2.5-pro") == {"thinking_level": "HIGH"}


def test_off_level_is_empty():
    assert llm.resolve_thinking_params(ThinkLevel.OFF, "openai/gpt-5.5") == {}


def test_resolve_model_provider_inference():
    assert llm.resolve_model("openai/gpt-5.5").provider == "openai"
    assert llm.resolve_model("anthropic/claude-opus-4-8").provider == "anthropic"
    assert llm.resolve_model("openrouter/anthropic/claude-opus-4-8").provider == "anthropic"
    assert llm.resolve_model("gemini/gemini-2.5-pro").provider == "google"
    # context window is always a positive int (fallback when unknown)
    assert llm.resolve_model("totally/unknown-model").context_window > 0


def test_cache_markers_anthropic_splits_system_at_boundary():
    msgs = [
        {"role": "system", "content": f"STABLE PREFIX{CACHE_BOUNDARY}volatile date line"},
        {"role": "user", "content": "hello"},
    ]
    llm.apply_cache_markers(msgs, "anthropic/claude-opus-4-8")
    blocks = msgs[0]["content"]
    assert isinstance(blocks, list)
    assert blocks[0]["text"] == "STABLE PREFIX"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == "volatile date line"
    assert "cache_control" not in blocks[1]
    # Trailing message gets the sliding breakpoint.
    assert msgs[-1]["cache_control"] == {"type": "ephemeral"}


def test_cache_markers_non_anthropic_consumes_boundary_no_markers():
    msgs = [
        {"role": "system", "content": f"STABLE{CACHE_BOUNDARY}volatile"},
        {"role": "user", "content": "hi"},
    ]
    llm.apply_cache_markers(msgs, "openai/gpt-5.5")
    # Boundary marker text removed; no cache_control anywhere.
    assert CACHE_BOUNDARY not in msgs[0]["content"]
    assert "STABLE" in msgs[0]["content"] and "volatile" in msgs[0]["content"]
    assert all("cache_control" not in m for m in msgs)


def test_normalize_extracts_text_tool_calls_usage():
    # Build a duck-typed litellm-style response object.
    class _Fn:
        name = "bash"
        arguments = '{"command": "echo hi"}'

    class _TC:
        id = "call_1"
        function = _Fn()

    class _Msg:
        content = "running it"
        tool_calls = [_TC()]

    class _Choice:
        message = _Msg()
        finish_reason = "tool_calls"

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 20
        total_tokens = 120

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    result = llm._normalize(_Resp())
    assert result.text == "running it"
    assert result.has_tool_calls
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].arguments == '{"command": "echo hi"}'
    assert result.finish_reason == "tool_calls"
    assert result.usage == {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
