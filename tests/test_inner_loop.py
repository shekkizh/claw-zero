"""Phase 7 — one activation runs a bash tool call, then delivers a text Message."""

import asyncio

from claw_zero import inner_loop, llm
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
        model="openai/gpt-5.5",
        system_prompt="(test system prompt)",
        messages=messages,
        tools=registry,
        memory_store=MemoryStore(base_dir=tmp_path),
        transcript=Transcript(base_dir=tmp_path),
        flush_state=FlushState(),
        incoming=incoming,
    )


def test_activation_runs_bash_then_delivers_message(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, "run `echo hello` and tell me the output")
    ctx.transcript.init_session(model=ctx.model)

    calls = {"n": 0}

    async def fake_call(model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # First turn: ask to run echo hello via the bash tool.
            return llm.LLMResult(
                text="",
                tool_calls=[llm.ToolCall(id="c1", name="bash", arguments='{"command": "echo hello"}')],
                finish_reason="tool_calls",
            )
        # Second turn: the model has seen the tool result and replies in text.
        # Confirm the tool result is actually present in the conversation.
        last_tool = [m for m in messages if m.get("role") == "tool"][-1]
        assert "hello" in last_tool["content"]
        return llm.LLMResult(text="The output was: hello", tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(llm, "call", fake_call)

    delivered = asyncio.run(inner_loop.run(ctx))

    assert isinstance(delivered, Message)
    assert delivered.sender == "claw-zero"
    assert delivered.recipient == "operator"
    assert "hello" in delivered.content
    assert calls["n"] == 2  # one tool turn + one reply turn

    # Transcript captured the assistant turns + the tool result.
    lines = ctx.transcript.path.read_text().splitlines()
    assert any('"role": "tool"' in l or '"role":"tool"' in l for l in lines)


def test_activation_with_no_tool_call_delivers_immediately(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, "just say hi")
    ctx.transcript.init_session(model=ctx.model)

    async def fake_call(model, messages, **kwargs):
        return llm.LLMResult(text="hi there", tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(llm, "call", fake_call)
    delivered = asyncio.run(inner_loop.run(ctx))
    assert delivered.content == "hi there"
    assert delivered.recipient == "operator"


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
