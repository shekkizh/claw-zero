"""Phase 2 - unit tests for llm.py's offline adapter pieces."""

import pytest

from claw_zero import llm
from claw_zero.llm import CACHE_BOUNDARY


def test_resolve_context_window_openai_only():
    assert llm.resolve_context_window("gpt-5.5") == 1_050_000
    assert llm.resolve_context_window("openai/gpt-5.5") == 1_050_000
    assert llm.resolve_context_window("future-model") == llm.DEFAULT_CONTEXT_TOKENS
    with pytest.raises(ValueError, match="OpenAI-only"):
        llm.resolve_context_window("anthropic/claude-opus-4-8")


def test_responses_input_converts_chat_history_without_mutating():
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
    items = llm._responses_input(msgs)
    assert items == [
        {"role": "assistant", "content": "old"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "send_message",
            "arguments": '{"to": "coder", "content": "go"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'},
        {"role": "user", "content": "hi"},
    ]
    assert len(msgs) == 3


def test_responses_input_replays_stored_response_items():
    response_items = [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {
            "type": "shell_call",
            "call_id": "call_1",
            "action": {"commands": ["pwd"], "timeout_ms": 120000, "max_output_length": 4096},
        },
    ]
    msgs = [
        {"role": "assistant", "content": "", "response_items": response_items},
        {
            "type": "shell_call_output",
            "call_id": "call_1",
            "max_output_length": 4096,
            "output": [{"stdout": "/tmp\n", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}],
        },
    ]
    assert llm._responses_input(msgs) == [
        *response_items,
        {
            "type": "shell_call_output",
            "call_id": "call_1",
            "max_output_length": 4096,
            "output": [{"stdout": "/tmp\n", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}],
        },
    ]


def test_build_openai_kwargs_maps_parameters():
    tool = {
        "type": "shell",
        "environment": {"type": "local"},
    }
    kwargs = llm._build_openai_kwargs(
        model="openai/gpt-5.5",
        messages=[{"role": "user", "content": "hi"}],
        system=f"SYS{CACHE_BOUNDARY}DYNAMIC",
        tools=[tool],
        max_tokens=123,
        temperature=1.0,
        timeout=30,
    )
    assert kwargs == {
        "model": "gpt-5.5",
        "input": [{"role": "user", "content": "hi"}],
        "instructions": "SYS\n\nDYNAMIC",
        "max_output_tokens": 123,
        "temperature": 1.0,
        "reasoning": {"effort": "xhigh"},
        "tools": [tool],
        "timeout": 30,
    }


def test_normalize_extracts_text_tool_calls_usage_and_items():
    class _Text:
        type = "output_text"
        text = "running it"
        annotations = []

    class _Msg:
        type = "message"
        content = [_Text()]

    class _Call:
        type = "function_call"
        call_id = "call_1"
        name = "send_message"
        arguments = '{"to": "coder", "content": "go"}'

    class _Action:
        commands = ["echo hi"]
        timeout_ms = 120000
        max_output_length = 4096

    class _Shell:
        type = "shell_call"
        call_id = "sh_1"
        action = _Action()
        status = "in_progress"

    class _Usage:
        input_tokens = 100
        output_tokens = 20
        total_tokens = 120

    class _Resp:
        output = [_Msg(), _Call(), _Shell()]
        usage = _Usage()
        status = "completed"
        incomplete_details = None

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
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "running it", "annotations": []}],
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "send_message",
            "arguments": '{"to": "coder", "content": "go"}',
        },
        {
            "type": "shell_call",
            "call_id": "sh_1",
            "action": {"commands": ["echo hi"], "timeout_ms": 120000, "max_output_length": 4096},
            "status": "in_progress",
        },
    ]


def test_normalize_appends_web_citation_sources():
    class _Citation:
        type = "url_citation"
        title = "Example"
        url = "https://example.com/report"
        start_index = 0
        end_index = 7

    class _Text:
        type = "output_text"
        text = "Current result."
        annotations = [_Citation()]

    class _Msg:
        type = "message"
        content = [_Text()]

    class _Search:
        type = "web_search_call"

        def model_dump(self, exclude_none=True):
            return {"type": "web_search_call", "id": "ws_1", "status": "completed"}

    class _Resp:
        output = [_Search(), _Msg()]
        usage = None
        status = "completed"
        incomplete_details = None

    result = llm._normalize(_Resp())
    assert result.text == "Current result.\n\nSources:\n- Example: https://example.com/report"
    assert result.response_items[0] == {"type": "web_search_call", "id": "ws_1", "status": "completed"}


def test_dump_item_preserves_web_search_call_action_without_model_dump():
    class _Action:
        type = "search"
        query = "latest AI news"

    class _Search:
        type = "web_search_call"
        id = "ws_1"
        status = "completed"
        action = _Action()

    assert llm._dump_item(_Search()) == {
        "type": "web_search_call",
        "id": "ws_1",
        "status": "completed",
        "action": {"type": "search", "query": "latest AI news"},
    }
