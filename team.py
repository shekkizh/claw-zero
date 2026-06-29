"""Team — the orchestrator for a flat mesh of self-owned agents.

A team is N ``Agent`` loops sharing one ``MessageBus``. Every participant has a
name and is addressed by it — agents and the human operator alike. Each agent has
its own memory/transcript/inbox and can message any other participant by name.
There is no lead and no hierarchy — coordination is emergent, via messages. A
single-agent run is just a team of one (the operator is the only external
participant); that is the path ``__main__`` takes when no extra agents are
requested, so the original behavior is preserved exactly.

Responsibilities:
  - Build each agent with local Shell plus the team toolset
    (``send_message`` + ``spawn_agent``) bound to the bus and that agent's id.
  - Register the operator (the human's stdio channel) on the bus.
  - Run one outer-loop task per agent, plus the operator's inbound pump and an
    optional self-tick source.
  - Spawn new teammates at runtime (the ``spawn_agent`` tool calls back here).
  - Tear down gracefully on stdin EOF: drain queued messages and let the
    in-flight activations finish before cancelling.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import outer_loop
from .config import ClawZeroConfig
from .messaging.bus import MessageBus
from .messaging.mailbox import Message
from .messaging.peer import Peer, StdioPeer, tick_source
from .outer_loop import Agent
from .runtime_state import load_team_state, write_team_state
from .source_identity import format_source_identity
from .tools.reload_harness import ReloadRequested
from .tools.registry import Tool
from .tools.send_message import SendMessageTool
from .tools.spawn_agent import SpawnAgentTool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _Member:
    """One agent on the team plus the asyncio task running its loop."""

    agent: Agent
    idle: asyncio.Event
    task: asyncio.Task | None = None


class Team:
    """Owns the bus, the roster, and the per-agent loop tasks."""

    def __init__(
        self,
        config: ClawZeroConfig,
        *,
        agents_md: str | None = None,
        allow_spawn: bool = True,
        allow_reload: bool = True,
        resume_runtime_state: bool = False,
    ) -> None:
        self._config = config
        self._agents_md = agents_md
        self._allow_spawn = allow_spawn
        self._allow_reload = allow_reload
        self._resume_runtime_state = resume_runtime_state
        self._suppress_team_state_save = resume_runtime_state
        self._saved_team_state = load_team_state(config.base_dir) if resume_runtime_state else None
        # Team-capable when there's a configured/saved roster beyond the primary,
        # OR an agent may bring teammates online at runtime. A run that is none
        # of those is the original single-agent claw-zero: baseline shell/search
        # tools, no team prose. Computed up front because it gates the cached
        # static prompt prefix.
        self._team_capable = bool(config.agents) or allow_spawn or self._saved_team_has_multiple_agents()
        self.bus = MessageBus()
        self._members: dict[str, _Member] = {}
        self._extra_tasks: list[asyncio.Task] = []
        self._started = False

    # -- construction --------------------------------------------------------

    def _team_tools(self, agent_id: str) -> list[Tool]:
        """The team-aware tools, bound to ``agent_id`` and the shared bus.

        Team tools appear only for a team-capable run (absence is the signal —
        a lone agent keeps no team prose). ``reload_harness`` is a baseline
        Agent tool, so it is not assembled here.
        """
        tools: list[Tool] = []
        if self._team_capable:
            tools.append(SendMessageTool(self.bus, agent_id))
            if self._allow_spawn:
                tools.append(SpawnAgentTool(self._spawn, agent_id))
        return tools

    def _build_agent(self, agent_id: str, model: str) -> Agent:
        """Create an ``Agent`` for ``agent_id`` and register its inbox."""
        self.bus.add_agent(agent_id)
        return Agent.create(
            agent_id=agent_id,
            model=model,
            base_dir=self._config.base_dir,
            auto_compact_token_limit=self._config.auto_compact_token_limit,
            tool_output_token_limit=self._config.tool_output_token_limit,
            compaction_threshold=self._config.compaction_threshold,
            max_tool_result_chars=self._config.max_tool_result_chars,
            agents_md=self._agents_md,
            context_window=self._config.context_window_tokens,
            extra_tools=self._team_tools(agent_id),
            include_reload_tool=self._allow_reload,
            resume_runtime_state=self._resume_runtime_state,
        )

    def add_agent(self, agent_id: str, model: str | None = None) -> Agent:
        """Register an agent on the team before start (the static roster path)."""
        if agent_id in self._members:
            return self._members[agent_id].agent
        agent = self._build_agent(agent_id, model or self._config.model)
        self._members[agent_id] = _Member(agent=agent, idle=asyncio.Event())
        if not self._suppress_team_state_save:
            self.save_team_state(reason="agent_added")
        return agent

    def add_peer(self, peer: Peer) -> None:
        """Register an external peer (the operator's stdio channel) on the bus."""
        self.bus.add_peer(peer)

    @property
    def agent_ids(self) -> list[str]:
        return list(self._members)

    def context_window_of(self, agent_id: str) -> int:
        return self._members[agent_id].agent.context_window

    def restore_saved_agents(self) -> None:
        """Restore saved spawned teammates after configured roster is present."""
        state = self._saved_team_state
        try:
            if state is None:
                return
            for member in state.get("members", []):
                if not isinstance(member, dict):
                    continue
                agent_id = member.get("id")
                model = member.get("model")
                if not isinstance(agent_id, str) or not agent_id.strip():
                    continue
                if agent_id == self._config.operator_id:
                    continue
                if model is not None and not isinstance(model, str):
                    model = None
                self.add_agent(agent_id, model or self._config.model)
        finally:
            self._suppress_team_state_save = False
            self.save_team_state(reason="runtime_state_restored")

    def _saved_team_has_multiple_agents(self) -> bool:
        if not isinstance(self._saved_team_state, dict):
            return False
        members = self._saved_team_state.get("members")
        if not isinstance(members, list):
            return False
        ids = {
            member.get("id")
            for member in members
            if isinstance(member, dict) and isinstance(member.get("id"), str)
        }
        return len(ids) > 1

    # -- runtime spawn (the spawn_agent tool calls this) ---------------------

    async def _spawn(
        self,
        *,
        new_id: str,
        model: str | None,
        brief: str | None,
        spawned_by: str,
    ) -> dict[str, Any]:
        """Bring a new teammate online while the team is running.

        Builds the agent, starts its loop task, and (if a brief was given)
        delivers it as the new agent's opening message — routed from the spawner,
        so the new agent replies to the right peer. Idempotent on id: spawning an
        existing id is rejected rather than silently re-homing its inbox.
        """
        if new_id in self._members:
            return {"success": False, "error": f"Agent {new_id!r} already exists on the team."}
        if not new_id.strip() or new_id in self.bus.peer_ids:
            return {"success": False, "error": f"Invalid or reserved agent id {new_id!r}."}

        agent = self._build_agent(new_id, model or self._config.model)
        member = _Member(agent=agent, idle=asyncio.Event())
        self._members[new_id] = member

        if self._started:
            member.task = asyncio.create_task(
                outer_loop.run(self.bus, agent, idle=member.idle)
            )

        if brief and brief.strip():
            await self.bus.route(
                Message(
                    sender=spawned_by,
                    recipient=new_id,
                    content=brief.strip(),
                    kind="message",
                    ts=_now_iso(),
                )
            )

        self.save_team_state(reason="agent_spawned")
        return {
            "success": True,
            "spawned": new_id,
            "model": agent.model,
            "note": "now online as a peer; it will reply on its own activations — do not poll",
        }

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch every registered agent's loop + the optional tick source.

        Idempotent guard via ``_started`` so a later runtime spawn knows to start
        its own task immediately rather than waiting for ``start``.
        """
        for member in self._members.values():
            if member.task is None:
                member.task = asyncio.create_task(
                    outer_loop.run(self.bus, member.agent, idle=member.idle)
                )
        if self._config.tick_seconds is not None:
            self._extra_tasks.append(
                asyncio.create_task(tick_source(self.bus, self._config.tick_seconds))
            )
        self._started = True

    def _all_idle(self) -> bool:
        return all(m.idle.is_set() for m in self._members.values())

    def save_team_state(self, *, reason: str) -> dict[str, Any]:
        return write_team_state(self._config.base_dir, self._team_state_payload(reason))

    def save_all_runtime_state(self, *, reason: str) -> None:
        for member in self._members.values():
            member.agent.save_runtime_state(reason=reason)
        self.save_team_state(reason=reason)

    def record_worker_start(self, source_identity: dict[str, Any]) -> None:
        note = format_source_identity(source_identity)
        for member in self._members.values():
            try:
                member.agent.memory_store.append_session(note)
            except RuntimeError:
                pass
            member.agent.transcript.append_message("system", note)

    def _team_state_payload(self, reason: str) -> dict[str, Any]:
        return {
            "reason": reason,
            "primary_agent_id": self._config.agent_id,
            "operator_id": self._config.operator_id,
            "allow_spawn": self._allow_spawn,
            "allow_reload": self._allow_reload,
            "members": [
                {"id": agent_id, "model": member.agent.model}
                for agent_id, member in self._members.items()
            ],
        }

    async def run_until_eof(self, operator: StdioPeer) -> None:
        """Run the team until the operator's stdin closes, then drain + tear down.

        Mirrors the original single-agent supervisor: pump the operator's stdin,
        and once it EOFs, let queued messages and in-flight activations finish
        (so no model call is cancelled mid-flight) before cancelling the loops.
        """
        self.start()
        inbound_task = asyncio.create_task(operator.inbound(self.bus))
        try:
            while True:
                tasks = [inbound_task, *self._agent_tasks()]
                done, _pending = await asyncio.wait(
                    tasks,
                    timeout=0.1,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    continue
                self._raise_finished_agent_errors(done, inbound_task=inbound_task)
                if inbound_task in done:
                    break

            # Graceful drain: done once no inbox has pending work AND every loop
            # is sitting idle. Agents can still be messaging each other, so we
            # wait for the whole mesh to settle, not just one loop.
            while self.bus.has_pending() or not self._all_idle():
                self._raise_finished_agent_errors(self._agent_tasks())
                await asyncio.sleep(0.1)
                if not self.bus.has_pending() and self._all_idle():
                    break
        except ReloadRequested:
            self.save_all_runtime_state(reason="reload_requested")
            raise
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Cancel all loop tasks and the tick source; await their teardown."""
        tasks = self._agent_tasks()
        tasks += self._extra_tasks
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _agent_tasks(self) -> list[asyncio.Task]:
        return [m.task for m in self._members.values() if m.task is not None]

    def _raise_finished_agent_errors(
        self,
        tasks: set[asyncio.Task] | list[asyncio.Task],
        *,
        inbound_task: asyncio.Task | None = None,
    ) -> None:
        for task in tasks:
            if task is inbound_task or not task.done() or task.cancelled():
                continue
            exc = task.exception()
            if isinstance(exc, ReloadRequested):
                raise exc
            if exc is not None:
                raise exc
            raise RuntimeError("agent loop exited unexpectedly")
