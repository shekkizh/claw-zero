"""Phase 7 — one activation runs local Shell, then delivers a text Message."""

import asyncio
import json

from claw_zero import inner_loop, llm
from claw_zero.context.compaction import CompactionResult
from claw_zero.context.transcript import Transcript
from claw_zero.inner_loop import ActivationContext
from claw_zero.memory.flush import FlushState
from claw_zero.memory.store import MemoryStore
from claw_zero.messaging.mailbox import Message
from claw_zero.tools.bash import BashTool
from claw_zero.tools.registry import build_tools


def _make_ctx(tmp_path, incoming_content: str) -> ActivationContext:
    registry = build_tools(BashTool(cwd=str(tmp_path)))
    incoming = Message(sender="operator", recipient="claw-zero", content=incoming_content)
    messages = [{"role": "user", "content": incoming_content}]
    return ActivationContext(
        model="gpt-5.5",
        system_prompt="(test system prompt)",
        messages=messages,
        tools=registry,
        memory_store=MemoryStore(base_dir=tmp_path),
        transcript=Transcript(base_dir=tmp_path),
        flush_state=FlushState(),
        incoming=incoming,
    )


def test_current_tokens_uses_api_input_token_floor(tmp_path):
    ctx = _make_ctx(tmp_path, "short")
    ctx.last_api_input_tokens = 9000
    assert inner_loop._current_tokens(ctx) == 9000


def test_activation_runs_shell_then_delivers_message(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, "run `echo hello` and tell me the output")
    ctx.transcript.init_session(model=ctx.model)

    calls = {"n": 0}

    async def fake_call(model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # First turn: ask to run echo hello via the local Shell tool.
            return llm.LLMResult(
                text="",
                shell_calls=[llm.ShellCall(id="c1", commands=["echo hello"], timeout_ms=120000)],
                finish_reason="tool_calls",
            )
        # Second turn: the model has seen the tool result and replies in text.
        # Confirm the tool result is actually present in the conversation.
        last_shell = [m for m in messages if m.get("type") == "shell_call_output"][-1]
        assert "hello" in json.dumps(last_shell)
        return llm.LLMResult(text="The output was: hello", tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(llm, "call", fake_call)

    delivered = asyncio.run(inner_loop.run(ctx))

    assert isinstance(delivered, Message)
    assert delivered.sender == "claw-zero"
    assert delivered.recipient == "operator"
    assert "hello" in delivered.content
    assert calls["n"] == 2  # one tool turn + one reply turn

    # Transcript captured the assistant turns + the shell result.
    lines = [json.loads(line) for line in ctx.transcript.path.read_text().splitlines()]
    assistant = next(
        entry["message"]
        for entry in lines
        if entry.get("type") == "message" and entry["message"].get("stopReason") == "tool_calls"
    )
    shell_entry = next(
        entry["message"]
        for entry in lines
        if entry.get("type") == "message" and entry["message"].get("role") == "shell"
    )
    assert assistant["toolCalls"] == [{
        "id": "c1",
        "type": "shell",
        "shell": {"commands": ["echo hello"], "timeout_ms": 120000},
    }]
    assert shell_entry["toolCallId"] == "c1"


def test_activation_with_no_tool_call_delivers_immediately(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, "just say hi")
    ctx.transcript.init_session(model=ctx.model)

    async def fake_call(model, messages, **kwargs):
        return llm.LLMResult(text="hi there", tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(llm, "call", fake_call)
    delivered = asyncio.run(inner_loop.run(ctx))
    assert delivered.content == "hi there"
    assert delivered.recipient == "operator"


def test_activation_compacts_after_final_reply_when_api_usage_over_budget(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, "finish and reply")
    ctx.transcript.init_session(model=ctx.model)
    ctx.context_window = 100
    ctx.auto_compact_token_limit = 50
    compact_calls = {"n": 0}

    async def fake_call(model, messages, **kwargs):
        return llm.LLMResult(
            text="done",
            tool_calls=[],
            finish_reason="stop",
            usage={"input_tokens": 80},
        )

    async def fake_compact(messages, model, context_window, **kwargs):
        compact_calls["n"] += 1
        return CompactionResult(
            summary="## Goal\nContinue from compacted state.",
            tokens_before=80,
            tokens_after=10,
            first_kept_message_index=len(messages),
            chunks_processed=1,
        )

    monkeypatch.setattr(llm, "call", fake_call)
    monkeypatch.setattr(inner_loop, "compact_messages", fake_compact)

    delivered = asyncio.run(inner_loop.run(ctx))

    assert delivered.content == "done"
    assert compact_calls["n"] == 1
    assert ctx.last_api_input_tokens == 0
    assert len(ctx.messages) == 1
    assert "Continue from compacted state" in ctx.messages[0]["content"]


def test_exact_count_can_prevent_estimate_only_compaction(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, "x" * 500)
    ctx.auto_compact_token_limit = 50

    async def fake_count_input_tokens(model, messages, **kwargs):
        return 40

    monkeypatch.setattr(llm, "count_input_tokens", fake_count_input_tokens)

    current = asyncio.run(inner_loop._current_tokens_for_budget(ctx))

    assert current == 40
    assert ctx.last_api_input_tokens == 40
    assert inner_loop._over_budget(ctx, current) is False


def test_activation_records_hosted_search_call_and_result(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, "search for current news")
    ctx.transcript.init_session(model=ctx.model)

    async def fake_call(model, messages, **kwargs):
        return llm.LLMResult(
            text="Current result.\n\nSources:\n- Example: https://example.com/report",
            finish_reason="completed",
            response_items=[
                {
                    "type": "web_search_call",
                    "id": "ws_1",
                    "status": "completed",
                    "action": {"type": "search", "query": "current news"},
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": "Current result.",
                        "annotations": [{
                            "type": "url_citation",
                            "start_index": 0,
                            "end_index": 14,
                            "url": "https://example.com/report",
                            "title": "Example",
                        }],
                    }],
                },
            ],
        )

    monkeypatch.setattr(llm, "call", fake_call)
    delivered = asyncio.run(inner_loop.run(ctx))

    assert "Current result" in delivered.content
    lines = [json.loads(line) for line in ctx.transcript.path.read_text().splitlines()]
    assistant = next(
        entry["message"]
        for entry in lines
        if entry.get("type") == "message" and entry["message"].get("role") == "assistant"
    )
    assert assistant["toolCalls"] == [{
        "id": "ws_1",
        "type": "web_search",
        "status": "completed",
        "action": {"type": "search", "query": "current news"},
    }]
    assert assistant["toolResults"][0]["type"] == "web_search_result"
    assert assistant["toolResults"][0]["toolCallId"] == "ws_1"
    assert assistant["toolResults"][0]["content"][0]["annotations"][0]["title"] == "Example"


def test_activation_records_reasoning_summary(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, "explain the repo state")
    ctx.transcript.init_session(model=ctx.model)

    async def fake_call(model, messages, **kwargs):
        return llm.LLMResult(
            text="Done.",
            finish_reason="completed",
            response_items=[
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{
                        "type": "summary_text",
                        "text": "Inspected transcript persistence and selected a schema-only change.",
                    }],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done.", "annotations": []}],
                },
            ],
        )

    monkeypatch.setattr(llm, "call", fake_call)
    delivered = asyncio.run(inner_loop.run(ctx))

    assert delivered.content == "Done."
    lines = [json.loads(line) for line in ctx.transcript.path.read_text().splitlines()]
    assistant = next(
        entry["message"]
        for entry in lines
        if entry.get("type") == "message" and entry["message"].get("role") == "assistant"
    )
    assert assistant["reasoningSummaries"] == [{
        "id": "rs_1",
        "summary": [{
            "type": "summary_text",
            "text": "Inspected transcript persistence and selected a schema-only change.",
        }],
    }]


def test_unknown_tool_is_reported_not_crashed(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, "do a thing")
    ctx.transcript.init_session(model=ctx.model)
    seq = iter([
        llm.LLMResult(text="", tool_calls=[llm.ToolCall(id="c1", name="nonexistent", arguments="{}")],
                      finish_reason="tool_calls"),
        llm.LLMResult(text="handled", tool_calls=[], finish_reason="stop"),
    ])

    async def fake_call(model, messages, **kwargs):
        return next(seq)

    monkeypatch.setattr(llm, "call", fake_call)
    delivered = asyncio.run(inner_loop.run(ctx))
    assert delivered.content == "handled"
    tool_msg = [m for m in ctx.messages if m.get("role") == "tool"][-1]
    assert "Unknown tool" in tool_msg["content"]
    lines = [json.loads(line) for line in ctx.transcript.path.read_text().splitlines()]
    assistant = next(
        entry["message"]
        for entry in lines
        if entry.get("type") == "message" and entry["message"].get("stopReason") == "tool_calls"
    )
    tool_entry = next(
        entry["message"]
        for entry in lines
        if entry.get("type") == "message" and entry["message"].get("role") == "tool"
    )
    assert assistant["toolCalls"][0]["function"]["name"] == "nonexistent"
    assert tool_entry["toolCallId"] == "c1"
