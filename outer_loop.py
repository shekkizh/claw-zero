"""Outer loop — one agent's self-owned loop that never returns.

Owns the durable cross-activation state for a single agent (the running
conversation, transcript, memory store, flush bookkeeping, tool registry) and
the system-prompt assembly. Each turn it waits for the next message **on its own
inbox** — from the operator, a teammate agent, or a self-tick — appends it to the
conversation, runs one inner-loop activation, and delivers the reply by routing
``reply.recipient`` through the shared ``MessageBus``. Then it loops, forever.

A *team* runs one of these loops per agent, all sharing one bus (see
``team.py``). A single-agent run is the degenerate case: one loop, one agent,
the operator as the only external participant. Nothing here branches on *who* the
sender is — routing is purely by recipient name (the equal-operator thesis).

"Sleep" on a tick is just an activation that returns empty text: the loop
delivers nothing and goes back to waiting. The agent itself decides that, per the
prompt's pacing guidance.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import inner_loop
from .context.token_estimation import estimate_message_tokens
from .context.transcript import Transcript
from .inner_loop import ActivationContext
from .llm import resolve_context_window
from .memory.flush import FlushState
from .memory.store import MemoryStore
from .messaging.bus import MessageBus
from .messaging.mailbox import Message
from .prompt import ContextFile, RuntimeContext, build_prompt
from .tools.bash import BashTool
from .tools.registry import Tool, ToolRegistry, build_tools


@dataclass
class Agent:
    """The durable, self-owned agent: config + cross-activation state.

    One ``Agent`` lives for the whole process (or, in a team, for as long as that
    teammate is online). Its ``messages`` list is the persistent conversation
    that the inner loop mutates and the compaction pipeline shrinks — claw-zero
    is long-running, so this is not rebuilt per activation.
    """

    agent_id: str
    model: str
    memory_store: MemoryStore
    transcript: Transcript
    tools: ToolRegistry
    context_window: int
    compaction_threshold: float = 0.8
    max_tool_result_chars: int = 16_000
    context_files: list[ContextFile] = field(default_factory=list)

    # Mutable cross-activation state.
    messages: list[dict[str, Any]] = field(default_factory=list)
    flush_state: FlushState = field(default_factory=FlushState)

    @classmethod
    def create(
        cls,
        *,
        agent_id: str,
        model: str,
        base_dir: str | None = None,
        compaction_threshold: float = 0.8,
        max_tool_result_chars: int = 16_000,
        cwd: str | None = None,
        agents_md: str | None = None,
        context_window: int | None = None,
        extra_tools: list[Tool] | None = None,
    ) -> "Agent":
        """Wire up an Agent with its memory store, transcript, and tools.

        ``agents_md`` (the loaded ``AGENTS.md`` text) is injected as a context
        file. ``extra_tools`` are appended after ``bash`` — the team tools
        (``send_message``, ``spawn_agent``) come in this way, so a single-agent
        run that passes none simply has the original one-tool surface. The
        session log and transcript session header are initialized here.
        ``context_window`` overrides the model-resolved window when given.
        """
        memory_store = MemoryStore(agent_id=agent_id, base_dir=base_dir)
        memory_store.init_session()
        transcript = Transcript(agent_id=agent_id, base_dir=base_dir)
        transcript.init_session(model=model)

        tools = build_tools(
            BashTool(cwd=cwd, max_output_chars=max_tool_result_chars),
            *(extra_tools or []),
        )

        context_files: list[ContextFile] = []
        if agents_md:
            context_files.append(ContextFile(path="AGENTS.md", content=agents_md))
        curated = memory_store.read_curated()
        if curated.strip():
            context_files.append(ContextFile(path="AGENT_MEMORY.md", content=curated))

        return cls(
            agent_id=agent_id,
            model=model,
            memory_store=memory_store,
            transcript=transcript,
            tools=tools,
            context_window=context_window or resolve_context_window(model),
            compaction_threshold=compaction_threshold,
            max_tool_result_chars=max_tool_result_chars,
            context_files=context_files,
        )

    def build_system_prompt(self, peers: list[str]) -> str:
        """Assemble the system prompt with current runtime context.

        Re-reads curated memory each turn so a freshly-written ``AGENT_MEMORY.md``
        is reflected. Volatile bits (date, peers, cwd) sit below the cache
        boundary; the static prefix stays byte-stable for prompt caching.
        ``peers`` is this agent's currently-reachable names (teammates + operator).
        """
        context_files = [cf for cf in self.context_files if cf.path != "AGENT_MEMORY.md"]
        curated = self.memory_store.read_curated()
        if curated.strip():
            context_files.append(ContextFile(path="AGENT_MEMORY.md", content=curated))

        runtime = RuntimeContext(
            date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            agent_id=self.agent_id,
            peers=peers,
            cwd=self._cwd(),
            memory_dir=str(self.memory_store.memory_dir.resolve()),
            curated_path=str((self.memory_store.agent_dir / self.memory_store.CURATED_FILE).resolve()),
        )
        return build_prompt(
            tool_summaries=self.tools.summaries,
            context_files=context_files,
            runtime=runtime,
            has_memory=True,
            # Gate on tool presence, not the live peer count: the Team section is
            # in the cached static prefix, so it must be stable for the agent's
            # whole life. ``send_message`` is registered iff this agent is on a
            # team, and tool registration never changes after creation.
            has_team="send_message" in self.tools.summaries,
        )

    def _cwd(self) -> str:
        bash = self.tools.handlers.get("bash")
        # The handler is BashTool.run (bound method); reach its instance's cwd.
        instance = getattr(bash, "__self__", None)
        return getattr(instance, "cwd", "") if instance is not None else ""


def _incoming_text(msg: Message) -> str:
    """Render an incoming message as the user-turn content for the model.

    The sender is named for context, but the loop does not *branch* on it — a
    human and an agent are formatted identically.
    """
    if msg.kind == "tick":
        return "<tick> You're awake. Look for useful work, or sleep (reply empty) if there's nothing to do."
    return f"[message from {msg.sender}]\n{msg.content}"


async def deliver(reply: Message, bus: MessageBus) -> bool:
    """Route a reply to its recipient via the bus.

    Returns True if delivered. An empty reply (a "sleep") is never delivered.
    A reply to an unknown recipient is dropped by the bus with a warning.
    """
    if not reply.content.strip():
        return False  # sleep — deliver nothing
    return await bus.route(reply)


async def run(
    bus: MessageBus,
    agent: Agent,
    *,
    idle: asyncio.Event | None = None,
) -> None:
    """One agent's forever loop: receive → activate → deliver. Never returns.

    Awaits ``agent``'s own inbox on the shared ``bus`` and routes each reply back
    through the bus, so a teammate's reply reaches another teammate's inbox the
    same way a reply reaches the operator. ``idle`` (optional) is set whenever this
    loop is blocked waiting with nothing in flight, and cleared while an
    activation runs — a supervisor can wait on it to tear down between
    activations rather than cancelling one mid-flight.
    """
    inbox = bus.inbox(agent.agent_id)
    while True:
        if idle is not None:
            idle.set()
        msg = await inbox.receive()
        if idle is not None:
            idle.clear()

        # On a tick with a real message already queued behind it, skip the tick
        # entirely and go straight to that message (no point waking up to think
        # when there's concrete work waiting). Otherwise the tick becomes an
        # activation and the agent decides whether to act or "sleep" (reply empty).
        if msg.kind == "tick" and inbox.has_pending():
            continue

        # Append the incoming message to the durable conversation.
        agent.messages.append({"role": "user", "content": _incoming_text(msg)})
        agent.transcript.append_message("user", _incoming_text(msg))

        system_prompt = agent.build_system_prompt(bus.reachable_from(agent.agent_id))
        ctx = ActivationContext(
            model=agent.model,
            system_prompt=system_prompt,
            messages=agent.messages,
            tools=agent.tools,
            memory_store=agent.memory_store,
            transcript=agent.transcript,
            flush_state=agent.flush_state,
            incoming=msg,
            context_window=agent.context_window,
            compaction_threshold=agent.compaction_threshold,
            max_tool_result_chars=agent.max_tool_result_chars,
            instructions_tokens=estimate_message_tokens({"role": "system", "content": system_prompt}),
        )

        reply = await inner_loop.run(ctx)
        await deliver(reply, bus)
        # loop forever; no exit condition
