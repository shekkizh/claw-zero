"""MessageBus — routing across agent inboxes and external peers."""

import asyncio

from claw_zero.messaging.bus import MessageBus
from claw_zero.messaging.mailbox import Message
from claw_zero.messaging.peer import parse_address


class RecordingPeer:
    def __init__(self, peer_id="operator"):
        self.id = peer_id
        self.delivered = []

    async def inbound(self, bus):
        return

    async def outbound(self, msg):
        self.delivered.append(msg)


def test_route_to_agent_inbox_and_external_peer():
    async def run():
        bus = MessageBus()
        inbox = bus.add_agent("coder")
        human = RecordingPeer("operator")
        bus.add_peer(human)

        assert await bus.route(Message(sender="operator", recipient="coder", content="build it"))
        assert inbox.has_pending()
        assert (await inbox.receive()).content == "build it"

        assert await bus.route(Message(sender="coder", recipient="operator", content="done"))
        assert human.delivered[0].content == "done"

    asyncio.run(run())


def test_unknown_recipient_is_dropped_not_crashed():
    async def run():
        bus = MessageBus()
        bus.add_agent("a")
        assert await bus.route(Message(sender="a", recipient="ghost", content="x")) is False

    asyncio.run(run())


def test_add_agent_is_idempotent():
    bus = MessageBus()
    first = bus.add_agent("a")
    second = bus.add_agent("a")
    assert first is second  # same inbox, not split
    assert bus.agent_ids == ["a"]


def test_reachable_from_excludes_self_includes_peers():
    bus = MessageBus()
    bus.add_agent("a")
    bus.add_agent("b")
    bus.add_peer(RecordingPeer("operator"))
    reachable = bus.reachable_from("a")
    assert "a" not in reachable
    assert "b" in reachable and "operator" in reachable


def test_has_pending_reflects_any_inbox():
    async def run():
        bus = MessageBus()
        bus.add_agent("a")
        bus.add_agent("b")
        assert not bus.has_pending()
        await bus.route(Message(sender="x", recipient="b", content="hi"))
        assert bus.has_pending()

    asyncio.run(run())


def test_parse_address_prefixes_and_default():
    known = ["coder", "planner"]
    assert parse_address("@coder do x", "claw-zero", known) == ("coder", "do x")
    assert parse_address("planner: think", "claw-zero", known) == ("planner", "think")
    # Unknown target → whole line goes to the default recipient.
    assert parse_address("note: a stray colon", "claw-zero", known) == ("claw-zero", "note: a stray colon")
    # No prefix → default recipient, content unchanged.
    assert parse_address("just text", "claw-zero", known) == ("claw-zero", "just text")
