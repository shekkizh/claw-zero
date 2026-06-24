"""Phase 8 — end-to-end outer loop: receive -> activate -> deliver; runs forever.

Now bus-routed: an agent awaits its own inbox on a shared MessageBus and routes
replies back through the bus. A single-agent run registers one agent + the human
peer; routing is by recipient (no sender special-casing).
"""

import asyncio

from claw_zero import llm, outer_loop
from claw_zero.messaging.bus import MessageBus
from claw_zero.messaging.mailbox import Message
from claw_zero.outer_loop import Agent, deliver


class FakePeer:
    """An external peer that records delivered messages instead of printing."""

    def __init__(self, peer_id="operator"):
        self.id = peer_id
        self.delivered: list[Message] = []

    async def inbound(self, bus):  # not used in these tests
        return

    async def outbound(self, msg: Message) -> None:
        self.delivered.append(msg)


def _agent(tmp_path, agent_id="claw-zero") -> Agent:
    return Agent.create(
        agent_id=agent_id,
        model="openai/gpt-5.5",
        base_dir=str(tmp_path),
        cwd=str(tmp_path),
        agents_md="# AGENTS.md\nhome doc",
    )


def test_two_messages_two_replies_and_never_exits(tmp_path, monkeypatch):
    async def fake_call(model, messages, **kwargs):
        # Echo the latest user message content back as a reply (no tool calls).
        last_user = [m for m in messages if m.get("role") == "user"][-1]
        return llm.LLMResult(text=f"ack: {last_user['content']}", tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(llm, "call", fake_call)

    async def scenario():
        bus = MessageBus()
        peer = FakePeer("operator")
        bus.add_peer(peer)
        agent = _agent(tmp_path)
        bus.add_agent(agent.agent_id)

        loop_task = asyncio.create_task(outer_loop.run(bus, agent))

        await bus.route(Message(sender="operator", recipient="claw-zero", content="first"))
        await bus.route(Message(sender="operator", recipient="claw-zero", content="second"))

        # Wait until both replies are delivered (the loop processes FIFO).
        for _ in range(200):
            if len(peer.delivered) >= 2:
                break
            await asyncio.sleep(0.01)

        assert not loop_task.done(), "outer loop must not exit on its own"
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        return peer.delivered

    delivered = asyncio.run(scenario())
    assert len(delivered) == 2
    assert all(m.sender == "claw-zero" and m.recipient == "operator" for m in delivered)
    assert "first" in delivered[0].content
    assert "second" in delivered[1].content


def test_tick_sleep_delivers_nothing(tmp_path, monkeypatch):
    async def fake_call(model, messages, **kwargs):
        # On a tick the agent decides there's nothing to do -> empty reply (sleep).
        return llm.LLMResult(text="", tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(llm, "call", fake_call)

    async def scenario():
        bus = MessageBus()
        peer = FakePeer("operator")
        bus.add_peer(peer)
        agent = _agent(tmp_path)
        bus.add_agent(agent.agent_id)
        loop_task = asyncio.create_task(outer_loop.run(bus, agent))

        await bus.route(Message(sender="self", recipient="claw-zero", content="", kind="tick"))
        await asyncio.sleep(0.2)

        assert not loop_task.done()
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        return peer.delivered

    delivered = asyncio.run(scenario())
    assert delivered == []  # sleeping delivers nothing


def test_deliver_routes_by_recipient(tmp_path):
    async def scenario():
        bus = MessageBus()
        a = FakePeer("operator")
        bus.add_peer(a)
        # An agent recipient routes to its inbox (not an external peer).
        inbox_b = bus.add_agent("agent-b")

        ok = await deliver(Message(sender="claw-zero", recipient="agent-b", content="hi"), bus)
        assert ok is True
        assert a.delivered == [] and inbox_b.has_pending()

        # Empty content is a sleep — never delivered.
        ok2 = await deliver(Message(sender="claw-zero", recipient="operator", content="   "), bus)
        assert ok2 is False
        assert a.delivered == []

        # A real reply to the human routes to its outbound.
        ok3 = await deliver(Message(sender="claw-zero", recipient="operator", content="done"), bus)
        assert ok3 is True and len(a.delivered) == 1

        # Unknown recipient -> dropped, not crashed.
        ok4 = await deliver(Message(sender="claw-zero", recipient="ghost", content="hi"), bus)
        assert ok4 is False

    asyncio.run(scenario())
