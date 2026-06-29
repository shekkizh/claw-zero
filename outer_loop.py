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
from .context.token_estimation import estimate_message_tokens, token_limit_to_char_cap
from .context.transcript import Transcript
from .inner_loop import ActivationContext
from .llm import DEFAULT_TOOL_OUTPUT_TOKENS, default_auto_compact_token_limit, resolve_context_window
from .memory.flush import FlushState
from .memory.store import MemoryStore
from .messaging.bus import MessageBus
from .messaging.mailbox import Message
from .prompt import ContextFile, RuntimeContext, build_prompt
from .runtime_state import load_runtime_state, save_agent_state, write_reload_state
from .tools.bash import BashTool
from .tools.reload_harness import ReloadRequested
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
    auto_compact_token_limit: int
    tool_output_token_limit: int = DEFAULT_TOOL_OUTPUT_TOKENS
    max_tool_result_chars: int = token_limit_to_char_cap(DEFAULT_TOOL_OUTPUT_TOKENS)
    context_files: list[ContextFile] = field(default_factory=list)
    last_api_input_tokens: int = 0

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
        auto_compact_token_limit: int | None = None,
        tool_output_token_limit: int = DEFAULT_TOOL_OUTPUT_TOKENS,
        compaction_threshold: float | None = None,
        max_tool_result_chars: int | None = None,
        cwd: str | None = None,
        agents_md: str | None = None,
        context_window: int | None = None,
        extra_tools: list[Tool] | None = None,
        resume_runtime_state: bool = False,
    ) -> "Agent":
        """Wire up an Agent with its memory store, transcript, and tools.

        ``agents_md`` (the loaded ``AGENTS.md`` text) is injected as a context
        file. ``extra_tools`` are appended after local Shell — the team tools
        (``send_message``, ``spawn_agent``) come in this way, so a single-agent
        run that passes none simply has Shell plus hosted web search. The
        session log and transcript session header are initialized here.
        ``context_window`` overrides the model-resolved window when given.
        """
        if resume_runtime_state:
            restored = cls.load(
                agent_id=agent_id,
                model=model,
                base_dir=base_dir,
                auto_compact_token_limit=auto_compact_token_limit,
                tool_output_token_limit=tool_output_token_limit,
                compaction_threshold=compaction_threshold,
                max_tool_result_chars=max_tool_result_chars,
                cwd=cwd,
                agents_md=agents_md,
                context_window=context_window,
                extra_tools=extra_tools,
            )
            if restored is not None:
                return restored

        memory_store = MemoryStore(agent_id=agent_id, base_dir=base_dir)
        memory_store.init_session()
        transcript = Transcript(agent_id=agent_id, base_dir=base_dir)
        transcript.init_session(model=model)

        resolved_context_window = context_window or resolve_context_window(model)
        compact_limit = auto_compact_token_limit
        if compact_limit is None:
            compact_limit = (
                int(resolved_context_window * compaction_threshold)
                if compaction_threshold is not None
                else default_auto_compact_token_limit(resolved_context_window)
            )
        if not 0 < compact_limit <= resolved_context_window:
            raise ValueError(
                "auto_compact_token_limit must be in (0, context_window], "
                f"got {compact_limit!r} for window {resolved_context_window!r}"
            )
        tool_output_char_cap = max_tool_result_chars or token_limit_to_char_cap(tool_output_token_limit)

        tools = build_tools(
            BashTool(cwd=cwd, max_output_chars=tool_output_char_cap),
            *(extra_tools or []),
        )

        return cls(
            agent_id=agent_id,
            model=model,
            memory_store=memory_store,
            transcript=transcript,
            tools=tools,
            context_window=resolved_context_window,
            auto_compact_token_limit=compact_limit,
            tool_output_token_limit=tool_output_token_limit,
            max_tool_result_chars=tool_output_char_cap,
            context_files=cls._context_files(memory_store, agents_md),
        )

    @classmethod
    def load(
        cls,
        *,
        agent_id: str,
        model: str,
        base_dir: str | None = None,
        auto_compact_token_limit: int | None = None,
        tool_output_token_limit: int = DEFAULT_TOOL_OUTPUT_TOKENS,
        compaction_threshold: float | None = None,
        max_tool_result_chars: int | None = None,
        cwd: str | None = None,
        agents_md: str | None = None,
        context_window: int | None = None,
        extra_tools: list[Tool] | None = None,
    ) -> "Agent | None":
        """Restore an Agent from JSON runtime state, building fresh tools."""
        memory_store = MemoryStore(agent_id=agent_id, base_dir=base_dir)
        state = load_runtime_state(memory_store.agent_dir)
        if state is None:
            return None

        session_log = state.get("session_log")
        if isinstance(session_log, str) and session_log:
            try:
                memory_store.resume_session(session_log)
            except (OSError, ValueError):
                memory_store.init_session()
        else:
            memory_store.init_session()

        transcript = Transcript(agent_id=agent_id, base_dir=base_dir)
        transcript_last_entry_id = state.get("transcript_last_entry_id")
        transcript.resume(
            transcript_last_entry_id if isinstance(transcript_last_entry_id, str) else None
        )

        resolved_context_window = _state_int(state, "context_window") or context_window or resolve_context_window(model)
        compact_limit = auto_compact_token_limit or _state_int(state, "auto_compact_token_limit")
        if compact_limit is None:
            compact_limit = (
                int(resolved_context_window * compaction_threshold)
                if compaction_threshold is not None
                else default_auto_compact_token_limit(resolved_context_window)
            )
        if not 0 < compact_limit <= resolved_context_window:
            raise ValueError(
                "auto_compact_token_limit must be in (0, context_window], "
                f"got {compact_limit!r} for window {resolved_context_window!r}"
            )

        restored_tool_tokens = _state_int(state, "tool_output_token_limit")
        effective_tool_tokens = (
            tool_output_token_limit
            if tool_output_token_limit != DEFAULT_TOOL_OUTPUT_TOKENS
            else restored_tool_tokens or tool_output_token_limit
        )
        tool_output_char_cap = (
            max_tool_result_chars
            or _state_int(state, "max_tool_result_chars")
            or token_limit_to_char_cap(effective_tool_tokens)
        )
        shell_cwd = state.get("shell_cwd") if isinstance(state.get("shell_cwd"), str) else None
        tools = build_tools(
            BashTool(cwd=shell_cwd or cwd, max_output_chars=tool_output_char_cap),
            *(extra_tools or []),
        )

        flush = state.get("flush_state") if isinstance(state.get("flush_state"), dict) else {}
        return cls(
            agent_id=agent_id,
            model=state.get("model") if isinstance(state.get("model"), str) else model,
            memory_store=memory_store,
            transcript=transcript,
            tools=tools,
            context_window=resolved_context_window,
            auto_compact_token_limit=compact_limit,
            tool_output_token_limit=effective_tool_tokens,
            max_tool_result_chars=tool_output_char_cap,
            context_files=cls._context_files(memory_store, agents_md),
            last_api_input_tokens=_state_int(state, "last_api_input_tokens") or 0,
            messages=state.get("messages") if isinstance(state.get("messages"), list) else [],
            flush_state=FlushState(
                compaction_count=flush.get("compaction_count", 0) if isinstance(flush.get("compaction_count", 0), int) else 0,
                flushed_at_compaction_count=(
                    flush.get("flushed_at_compaction_count")
                    if isinstance(flush.get("flushed_at_compaction_count"), int)
                    else None
                ),
            ),
        )

    @staticmethod
    def _context_files(memory_store: MemoryStore, agents_md: str | None) -> list[ContextFile]:
        context_files: list[ContextFile] = []
        if agents_md:
            context_files.append(ContextFile(path="AGENTS.md", content=agents_md))
        curated = memory_store.read_curated()
        if curated.strip():
            context_files.append(ContextFile(path="AGENT_MEMORY.md", content=curated))
        return context_files

    def save_runtime_state(self, *, reason: str) -> dict[str, Any]:
        return save_agent_state(self, reason=reason)

    def record_reload_request(self, request: ReloadRequested) -> None:
        note = (
            f"reload_harness requested. reason={request.reason!r}; "
            f"tests_run={request.tests_run or 'not specified'!r}; "
            f"summary={request.summary or ''!r}"
        )
        try:
            self.memory_store.append_session(note)
        except RuntimeError:
            pass
        self.transcript.append_message("system", note)
        state_payload = self.save_runtime_state(reason="reload_requested")
        write_reload_state(self, request, state_payload=state_payload)

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
        return getattr(self.tools.shell_tool, "cwd", "") if self.tools.shell_tool is not None else ""


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
            auto_compact_token_limit=agent.auto_compact_token_limit,
            tool_output_token_limit=agent.tool_output_token_limit,
            instructions_tokens=estimate_message_tokens({"role": "system", "content": system_prompt}),
            last_api_input_tokens=agent.last_api_input_tokens,
        )

        try:
            reply = await inner_loop.run(ctx)
        except ReloadRequested as exc:
            exc.agent_id = agent.agent_id
            agent.last_api_input_tokens = ctx.last_api_input_tokens
            agent.record_reload_request(exc)
            raise
        agent.last_api_input_tokens = ctx.last_api_input_tokens
        await deliver(reply, bus)
        agent.save_runtime_state(reason="activation_complete")
        # loop forever; no exit condition


def _state_int(state: dict[str, Any], key: str) -> int | None:
    value = state.get(key)
    return value if isinstance(value, int) and value > 0 else None
