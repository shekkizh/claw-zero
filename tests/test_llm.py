"""Unit tests for llm.py's Cerebras adapter pieces."""

import json

import pytest

from claw_zero import llm
from claw_zero.llm import CACHE_BOUNDARY


def test_resolve_context_window_cerebras_only():
    assert llm.resolve_context_window("gpt-oss-120b") == 128_000
    assert llm.resolve_context_window("cerebras/gpt-oss-120b") == 128_000
    assert llm.resolve_context_window("future-model") == llm.DEFAULT_CONTEXT_TOKENS
    with pytest.raises(ValueError, match="Cerebras-only"):
        llm.resolve_context_window("anthropic/claude-opus-4-8")


def test_chat_messages_converts_tool_history_without_mutating():
    msgs = [
        {
            "role": "assistant",
            "content": "old",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "send_message", "arguments": '{"to": "coder", "content": "go"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
        {"role": "user", "content": "hi"},
    ]
    items = llm._chat_messages(msgs)
    assert items == [
        {
            "role": "assistant",
            "content": "old",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "send_message", "arguments": '{"to": "coder", "content": "go"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
        {"role": "user", "content": "hi"},
    ]
    assert len(msgs) == 3


def test_chat_messages_replays_stored_shell_response_items():
    response_items = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "shell",
                        "arguments": json.dumps({
                            "commands": ["pwd"],
                            "timeout_ms": 120000,
                            "max_output_length": 4096,
                        }),
                    },
                }
            ],
        }
    ]
    shell_output = [{"stdout": "/tmp\n", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}]
    msgs = [
        {"role": "assistant", "content": "", "response_items": response_items},
        {
            "type": "shell_call_output",
            "call_id": "call_1",
            "max_output_length": 4096,
            "output": shell_output,
        },
    ]
    assert llm._chat_messages(msgs) == [
        response_items[0],
        {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(shell_output, ensure_ascii=False)},
    ]


def test_build_cerebras_kwargs_maps_parameters_and_tools():
    shell_tool = {"type": "shell", "environment": {"type": "local"}}
    function_tool = {
        "type": "function",
        "name": "send_message",
        "description": "Send a message.",
        "parameters": {"type": "object", "properties": {"to": {"type": "string"}}},
    }
    kwargs = llm._build_cerebras_kwargs(
        model="cerebras/gpt-oss-120b",
        messages=[{"role": "user", "content": "hi"}],
        system=f"SYS{CACHE_BOUNDARY}DYNAMIC",
        tools=[shell_tool, function_tool, {"type": "web_search"}],
        max_tokens=123,
        temperature=1.0,
        top_p=None,
        timeout=30,
    )
    assert kwargs["model"] == "gpt-oss-120b"
    assert kwargs["messages"] == [
        {"role": "system", "content": "SYS\n\nDYNAMIC"},
        {"role": "user", "content": "hi"},
    ]
    assert kwargs["max_completion_tokens"] == 123
    assert kwargs["temperature"] == 1.0
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["reasoning_format"] == "hidden"
    assert kwargs["parallel_tool_calls"] is True
    assert kwargs["timeout"] == 30
    assert "top_p" not in kwargs  # no recommendation for gpt-oss-120b
    assert [tool["function"]["name"] for tool in kwargs["tools"]] == ["shell", "send_message"]
    assert kwargs["tools"][0]["function"]["parameters"]["required"] == ["commands"]


def test_build_cerebras_kwargs_applies_gemma4_recommended_top_p():
    kwargs = llm._build_cerebras_kwargs(
        model="gemma-4-31b",
        messages=[{"role": "user", "content": "hi"}],
        system=None,
        tools=None,
        max_tokens=1000,
        temperature=1.0,
        top_p=None,
        timeout=None,
    )
    assert kwargs["top_p"] == 0.95  # Cerebras recommended default
    assert kwargs["reasoning_effort"] == "high"
    assert "reasoning_format" not in kwargs  # hidden not supported for Gemma 4

    # Explicit top_p from caller overrides the recommendation
    kwargs2 = llm._build_cerebras_kwargs(
        model="gemma-4-31b",
        messages=[{"role": "user", "content": "hi"}],
        system=None,
        tools=None,
        max_tokens=1000,
        temperature=1.0,
        top_p=0.8,
        timeout=None,
    )
    assert kwargs2["top_p"] == 0.8


def test_resolve_max_output_tokens():
    assert llm.resolve_max_output_tokens("gemma-4-31b") == 40_000
    assert llm.resolve_max_output_tokens("gpt-oss-120b") == llm.DEFAULT_MAX_OUTPUT_TOKENS
    assert llm.resolve_max_output_tokens("future-model") == llm.DEFAULT_MAX_OUTPUT_TOKENS


def test_gemma4_context_window():
    assert llm.resolve_context_window("gemma-4-31b") == 131_000


def test_normalize_extracts_text_tool_calls_shell_calls_usage_and_items():
    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _Call:
        type = "function"

        def __init__(self, id, name, arguments):
            self.id = id
            self.function = _Fn(name, arguments)

    class _Msg:
        content = "running it"
        tool_calls = [
            _Call("call_1", "send_message", '{"to": "coder", "content": "go"}'),
            _Call("sh_1", "shell", json.dumps({
                "commands": ["echo hi"],
                "timeout_ms": 120000,
                "max_output_length": 4096,
            })),
        ]

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
    assert result.tool_calls[0].name == "send_message"
    assert result.tool_calls[0].arguments == '{"to": "coder", "content": "go"}'
    assert result.shell_calls[0] == llm.ShellCall(
        id="sh_1",
        commands=["echo hi"],
        timeout_ms=120000,
        max_output_length=4096,
    )
    assert result.finish_reason == "tool_calls"
    assert result.usage == {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    assert result.response_items == [
        {
            "role": "assistant",
            "content": "running it",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "send_message", "arguments": '{"to": "coder", "content": "go"}'},
                },
                {
                    "id": "sh_1",
                    "type": "function",
                    "function": {
                        "name": "shell",
                        "arguments": json.dumps({
                            "commands": ["echo hi"],
                            "timeout_ms": 120000,
                            "max_output_length": 4096,
                        }),
                    },
                },
            ],
        }
    ]


def test_normalize_maps_usage_details():
    class _Details:
        cached_tokens = 11
        reasoning_tokens = 7

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 20
        total_tokens = 120
        prompt_tokens_details = _Details()
        completion_tokens_details = _Details()

    class _Msg:
        content = "done"
        tool_calls = []

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    result = llm._normalize(_Resp())
    assert result.text == "done"
    assert result.finish_reason == "stop"
    assert result.usage == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cached_input_tokens": 11,
        "reasoning_output_tokens": 7,
    }


def test_count_input_tokens_is_local_estimate():
    count = __import__("asyncio").run(llm.count_input_tokens(
        "gpt-oss-120b",
        [{"role": "user", "content": "hello"}],
        system="sys",
        tools=[{"type": "shell"}],
    ))
    assert isinstance(count, int) and count > 0
