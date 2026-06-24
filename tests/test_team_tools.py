"""send_message + spawn_agent tools — validation and routing behavior."""

import asyncio

from claw_zero.messaging.bus import MessageBus
from claw_zero.tools.send_message import SendMessageTool
from claw_zero.tools.spawn_agent import SpawnAgentTool


class RecordingPeer:
    def __init__(self, peer_id="operator"):
        self.id = peer_id
        self.delivered = []

    async def inbound(self, bus):
        return

    async def outbound(self, msg):
        self.delivered.append(msg)


def _bus_with(agents, peer_id="operator"):
    bus = MessageBus()
    inboxes = {a: bus.add_agent(a) for a in agents}
    peer = RecordingPeer(peer_id)
    bus.add_peer(peer)
    return bus, inboxes, peer


def test_send_message_to_teammate():
    async def run():
        bus, inboxes, _ = _bus_with(["planner", "coder"])
        tool = SendMessageTool(bus, "planner")
        res = await tool.run({"to": "coder", "content": "start task 1"})
        assert res["success"] and res["delivered_to"] == ["coder"]
        assert (await inboxes["coder"].receive()).content == "start task 1"

    asyncio.run(run())


def test_send_message_to_operator():
    async def run():
        bus, _, human = _bus_with(["planner"])
        tool = SendMessageTool(bus, "planner")
        res = await tool.run({"to": "operator", "content": "FYI"})
        assert res["success"]
        assert human.delivered[0].content == "FYI" and human.delivered[0].sender == "planner"

    asyncio.run(run())


def test_send_message_rejects_self_and_unknown():
    async def run():
        bus, _, _ = _bus_with(["planner", "coder"])
        tool = SendMessageTool(bus, "planner")
        assert (await tool.run({"to": "planner", "content": "x"}))["success"] is False
        unknown = await tool.run({"to": "ghost", "content": "x"})
        assert unknown["success"] is False and "coder" in unknown["reachable"]
        # Empty fields are rejected.
        assert (await tool.run({"to": "coder", "content": "  "}))["success"] is False

    asyncio.run(run())


def test_broadcast_hits_all_other_agents_not_operator():
    async def run():
        bus, inboxes, human = _bus_with(["a", "b", "c"])
        tool = SendMessageTool(bus, "a")
        res = await tool.run({"to": "*", "content": "standup"})
        assert res["success"] and set(res["delivered_to"]) == {"b", "c"}
        assert inboxes["b"].has_pending() and inboxes["c"].has_pending()
        assert not inboxes["a"].has_pending()  # never to self
        assert human.delivered == []  # broadcast excludes external peers

    asyncio.run(run())


def test_spawn_agent_invokes_callback_with_args():
    async def run():
        captured = {}

        async def fake_spawn(*, new_id, model, brief, spawned_by):
            captured.update(new_id=new_id, model=model, brief=brief, spawned_by=spawned_by)
            return {"success": True, "spawned": new_id}

        tool = SpawnAgentTool(fake_spawn, "planner")
        res = await tool.run({"id": "researcher", "brief": "dig into X"})
        assert res["success"] and res["spawned"] == "researcher"
        assert captured == {
            "new_id": "researcher", "model": None, "brief": "dig into X", "spawned_by": "planner",
        }
        # Empty id is rejected before the callback.
        assert (await tool.run({"id": ""}))["success"] is False

    asyncio.run(run())
