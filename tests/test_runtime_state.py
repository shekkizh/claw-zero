import asyncio
import json

import pytest

from claw_zero import llm, outer_loop
from claw_zero.memory.flush import FlushState
from claw_zero.messaging.bus import MessageBus
from claw_zero.messaging.mailbox import Message
from claw_zero.outer_loop import Agent
from claw_zero.runtime_state import (
    RELOAD_STATE_FILE,
    RUNTIME_STATE_FILE,
    mark_reload_continue_enqueued,
    pending_reload_continue,
)
from claw_zero.tools.reload_harness import ReloadRequested


class FakePeer:
    def __init__(self, peer_id="operator"):
        self.id = peer_id
        self.delivered: list[Message] = []

    async def inbound(self, bus):
        return

    async def outbound(self, msg: Message) -> None:
        self.delivered.append(msg)


def test_agent_runtime_state_round_trips_json_and_resumes_paths(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    agent = Agent.create(
        agent_id="claw-zero",
        model="gpt-5.5",
        base_dir=str(tmp_path),
        cwd=str(workdir),
        agents_md="# home",
        context_window=1000,
        auto_compact_token_limit=600,
    )
    transcript_entry = agent.transcript.append_message("user", "before save")
    agent.messages = [
        {"role": "user", "content": "remember alpha"},
        {
            "role": "assistant",
            "content": "",
            "response_items": [{
                "type": "function_call",
                "call_id": "c1",
                "name": "noop",
                "arguments": "{}",
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    agent.flush_state = FlushState(compaction_count=2, flushed_at_compaction_count=1)
    agent.last_api_input_tokens = 123

    first_session = agent.memory_store.current_session_path
    payload = agent.save_runtime_state(reason="test")
    state_path = tmp_path / "claw-zero" / RUNTIME_STATE_FILE

    assert json.loads(state_path.read_text())["messages"] == agent.messages
    assert payload["reason"] == "test"

    restored = Agent.create(
        agent_id="claw-zero",
        model="gpt-5.5",
        base_dir=str(tmp_path),
        cwd=str(tmp_path),
        agents_md="# home",
        resume_runtime_state=True,
    )

    assert restored.messages == agent.messages
    assert restored.flush_state == agent.flush_state
    assert restored.last_api_input_tokens == 123
    assert restored.memory_store.current_session_path == first_session
    assert restored.transcript.last_entry_id == transcript_entry
    assert restored._cwd() == str(workdir)

    restored.memory_store.append_session("after restore")
    assert len(list((tmp_path / "claw-zero" / "memory").glob("session-*.md"))) == 1


def test_outer_loop_saves_runtime_state_after_activation(tmp_path, monkeypatch):
    async def fake_call(model, messages, **kwargs):
        return llm.LLMResult(text="done", finish_reason="stop")

    monkeypatch.setattr(llm, "call", fake_call)

    async def scenario():
        bus = MessageBus()
        peer = FakePeer()
        bus.add_peer(peer)
        agent = Agent.create(agent_id="claw-zero", model="gpt-5.5", base_dir=str(tmp_path))
        bus.add_agent(agent.agent_id)
        task = asyncio.create_task(outer_loop.run(bus, agent))
        await bus.route(Message(sender="operator", recipient="claw-zero", content="work"))
        for _ in range(200):
            if peer.delivered:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    payload = json.loads((tmp_path / "claw-zero" / RUNTIME_STATE_FILE).read_text())
    assert payload["reason"] == "activation_complete"
    assert payload["messages"][-1]["content"] == "done"


def test_reload_harness_records_paired_tool_result_and_metadata(tmp_path, monkeypatch):
    async def fake_call(model, messages, **kwargs):
        return llm.LLMResult(
            text="",
            tool_calls=[llm.ToolCall(
                id="reload-1",
                name="reload_harness",
                arguments=json.dumps({
                    "reason": "pick up edited source",
                    "tests_run": "pytest tests/test_runtime_state.py",
                    "summary": "runtime state slice",
                }),
            )],
            finish_reason="tool_calls",
        )

    monkeypatch.setattr(llm, "call", fake_call)

    async def scenario():
        bus = MessageBus()
        agent = Agent.create(
            agent_id="claw-zero",
            model="gpt-5.5",
            base_dir=str(tmp_path),
        )
        bus.add_agent(agent.agent_id)
        task = asyncio.create_task(outer_loop.run(bus, agent))
        await bus.route(Message(sender="operator", recipient="claw-zero", content="reload now"))
        for _ in range(200):
            if task.done():
                break
            await asyncio.sleep(0.01)
        with pytest.raises(ReloadRequested):
            await task
        return agent

    agent = asyncio.run(scenario())
    tool_msg = [m for m in agent.messages if m.get("role") == "tool"][-1]
    assert tool_msg["tool_call_id"] == "reload-1"
    assert "reload_requested" in tool_msg["content"]

    runtime = json.loads((tmp_path / "claw-zero" / RUNTIME_STATE_FILE).read_text())
    reload_state = json.loads((tmp_path / "claw-zero" / RELOAD_STATE_FILE).read_text())
    assert runtime["reason"] == "reload_requested"
    assert runtime["messages"][-1] == tool_msg
    assert reload_state["reason"] == "pick up edited source"
    assert reload_state["exit_code"] == 75


def test_pending_reload_continue_is_marked_once(tmp_path):
    reload_path = tmp_path / "claw-zero" / RELOAD_STATE_FILE
    reload_path.parent.mkdir()
    reload_path.write_text(json.dumps({
        "version": 1,
        "requested_at": "2026-06-29T00:00:00+00:00",
        "agent_id": "claw-zero",
        "reason": "test reload",
    }))

    pending = pending_reload_continue(tmp_path)
    assert pending is not None
    assert pending["agent_id"] == "claw-zero"
    assert pending["path"] == str(reload_path)

    marked = mark_reload_continue_enqueued(
        pending["path"],
        sender="operator",
        recipient="claw-zero",
        content="continue",
    )
    assert marked is not None
    assert marked["continue_message"] == {
        "sender": "operator",
        "recipient": "claw-zero",
        "content": "continue",
    }
    assert pending_reload_continue(tmp_path) is None
