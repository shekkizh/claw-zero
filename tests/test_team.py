"""Team — multi-agent e2e: a teammate hands off via send_message; runtime spawn.

These drive real outer-loop tasks with a faked llm.call that scripts each
agent's behavior by id, so we exercise bus routing, the send_message tool, and
runtime spawning end-to-end without a model.
"""

import asyncio
import json

from claw_zero import llm
from claw_zero.config import ClawZeroConfig
from claw_zero.messaging.mailbox import Message
from claw_zero.messaging.peer import StdioPeer
from claw_zero.team import Team


def _config(tmp_path, **kw) -> ClawZeroConfig:
    return ClawZeroConfig(base_dir=str(tmp_path), **kw)


def test_two_static_agents_handoff(tmp_path, monkeypatch):
    """planner receives a human task, hands it to coder via send_message; coder
    replies to the human. Exercises agent->agent routing through the bus."""

    async def fake_call(model, messages, system=None, tools=None, **kwargs):
        # Identify which agent this is from its system prompt's agent id line.
        is_planner = "Your name: planner" in (system or "")
        last_user = [m for m in messages if m.get("role") == "user"][-1]["content"]

        if is_planner:
            if "build a parser" in last_user and "delivered_to" not in last_user:
                # First human task: delegate to coder via send_message, then reply.
                if not any(m.get("role") == "tool" for m in messages[-2:]):
                    return llm.LLMResult(
                        text="",
                        tool_calls=[llm.ToolCall(
                            id="t1", name="send_message",
                            arguments=json.dumps({"to": "coder", "content": "please build a parser"}),
                        )],
                        finish_reason="tool_calls",
                    )
            return llm.LLMResult(text="delegated to coder", tool_calls=[], finish_reason="stop")

        # coder: gets the message from planner, replies (which goes back to planner).
        return llm.LLMResult(text="parser built", tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(llm, "call", fake_call)

    async def scenario():
        config = _config(tmp_path, agents=["coder"], agent_id="planner")
        team = Team(config, agents_md="# home")
        team.add_agent("planner")
        team.add_agent("coder")
        human = StdioPeer(peer_id="operator", default_recipient="planner")
        team.add_peer(human)
        team.start()

        # Human asks planner to build a parser.
        await team.bus.route(Message(sender="operator", recipient="planner", content="build a parser"))

        # Let the mesh settle: planner delegates -> coder works -> coder replies to planner.
        for _ in range(300):
            await asyncio.sleep(0.01)
            if not team.bus.has_pending() and team._all_idle():
                break

        await team.shutdown()
        return team

    team = asyncio.run(scenario())
    # coder must have received planner's delegated message in its transcript history.
    coder = team._members["coder"].agent
    assert any("please build a parser" in (m.get("content") or "") for m in coder.messages)


def test_runtime_spawn_brings_agent_online(tmp_path, monkeypatch):
    """planner spawns a 'researcher' with a brief; the new agent runs and gets it."""

    async def fake_call(model, messages, system=None, tools=None, **kwargs):
        is_planner = "Your name: planner" in (system or "")
        if is_planner and not any(m.get("role") == "tool" for m in messages):
            return llm.LLMResult(
                text="",
                tool_calls=[llm.ToolCall(
                    id="s1", name="spawn_agent",
                    arguments=json.dumps({"id": "researcher", "brief": "research topic Z"}),
                )],
                finish_reason="tool_calls",
            )
        return llm.LLMResult(text="ok", tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(llm, "call", fake_call)

    async def scenario():
        config = _config(tmp_path, agent_id="planner")  # allow_spawn defaults True
        team = Team(config, agents_md="# home")
        team.add_agent("planner")
        human = StdioPeer(peer_id="operator", default_recipient="planner")
        team.add_peer(human)
        team.start()

        await team.bus.route(Message(sender="operator", recipient="planner", content="kick off"))
        for _ in range(300):
            await asyncio.sleep(0.01)
            if "researcher" in team.agent_ids and not team.bus.has_pending() and team._all_idle():
                break

        await team.shutdown()
        return team

    team = asyncio.run(scenario())
    assert "researcher" in team.agent_ids
    researcher = team._members["researcher"].agent
    assert any("research topic Z" in (m.get("content") or "") for m in researcher.messages)


def test_single_agent_run_has_no_team_tools(tmp_path):
    """A lone agent (no roster, no spawn) keeps only the baseline tools."""
    config = _config(tmp_path, agent_id="solo")
    team = Team(config, agents_md="# home", allow_spawn=False)
    agent = team.add_agent("solo")
    assert set(agent.tools.summaries) == {"shell", "web_search"}
    # And a team-capable run does surface the team tools.
    team2 = Team(_config(tmp_path, agent_id="lead", agents=["helper"]), agents_md="# home")
    lead = team2.add_agent("lead")
    assert "send_message" in lead.tools.summaries and "spawn_agent" in lead.tools.summaries


def test_reload_tool_is_supervisor_gated_not_team_gated(tmp_path):
    config = _config(tmp_path, agent_id="solo")
    team = Team(config, agents_md="# home", allow_spawn=False, allow_reload=True)
    agent = team.add_agent("solo")
    assert "reload_harness" in agent.tools.summaries
    assert "send_message" not in agent.tools.summaries
    assert "spawn_agent" not in agent.tools.summaries


def test_resume_restores_spawned_teammate_roster(tmp_path):
    async def scenario():
        config = _config(tmp_path, agent_id="planner")
        team = Team(config, agents_md="# home")
        team.add_agent("planner")
        result = await team._spawn(
            new_id="researcher",
            model=None,
            brief=None,
            spawned_by="planner",
        )
        assert result["success"] is True

        resumed = Team(config, agents_md="# home", resume_runtime_state=True)
        resumed.add_agent("planner")
        resumed.restore_saved_agents()
        return resumed.agent_ids, resumed._members["planner"].agent.tools.summaries

    agent_ids, summaries = asyncio.run(scenario())
    assert agent_ids == ["planner", "researcher"]
    assert "send_message" in summaries


def test_saved_roster_is_team_capable_even_when_spawn_disabled(tmp_path):
    async def seed():
        config = _config(tmp_path, agent_id="planner")
        team = Team(config, agents_md="# home")
        team.add_agent("planner")
        await team._spawn(new_id="researcher", model=None, brief=None, spawned_by="planner")

    asyncio.run(seed())

    config = _config(tmp_path, agent_id="planner", allow_spawn=False)
    resumed = Team(config, agents_md="# home", allow_spawn=False, resume_runtime_state=True)
    planner = resumed.add_agent("planner")
    resumed.restore_saved_agents()

    assert resumed.agent_ids == ["planner", "researcher"]
    assert "send_message" in planner.tools.summaries
    assert "spawn_agent" not in planner.tools.summaries
