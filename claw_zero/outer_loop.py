"""Outer loop — the self-owned loop that never returns.

Owns the durable cross-activation state (the running conversation, transcript,
memory store, flush bookkeeping, tool registry) and the system-prompt assembly.
It waits for the next message — from a human peer, an agent peer, or a self-tick
— appends it to the conversation, runs one inner-loop activation, and delivers
the reply by routing ``reply.recipient`` to that peer's ``outbound()``. Then it
loops, forever. There is no exit condition.

"Sleep" on a tick is just an activation that returns empty text: the loop
delivers nothing and goes back to waiting. The agent itself decides that, per the
prompt's pacing guidance.
"""

from __future__ import annotations

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
from .messaging.mailbox import Mailbox, Message
from .messaging.peer import Peer
from .prompt import ContextFile, RuntimeContext, build_prompt
from .tools.bash import BashTool
from .tools.registry import ToolRegistry, build_tools


@dataclass
class Agent:
    """The durable, self-owned agent: config + cross-activation state.

    One ``Agent`` lives for the whole process. Its ``messages`` list is the
    persistent conversation that the inner loop mutates and the compaction
    pipeline shrinks — claw-zero is long-running, so this is not rebuilt per
    activation.
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
    ) -> "Agent":
        """Wire up an Agent with its memory store, transcript, and bash tool.

        ``agents_md`` (the loaded ``AGENTS.md`` text) is injected as a context
        file. The session log and transcript session header are initialized here.
        """
        memory_store = MemoryStore(agent_id=agent_id, base_dir=base_dir)
        memory_store.init_session()
        transcript = Transcript(agent_id=agent_id, base_dir=base_dir)
        transcript.init_session(model=model)

        tools = build_tools(BashTool(cwd=cwd, max_output_chars=max_tool_result_chars))

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
            context_window=resolve_context_window(model),
            compaction_threshold=compaction_threshold,
            max_tool_result_chars=max_tool_result_chars,
            context_files=context_files,
        )

    def build_system_prompt(self, peers: list[str]) -> str:
        """Assemble the system prompt with current runtime context.

        Re-reads curated memory each turn so a freshly-written ``AGENT_MEMORY.md``
        is reflected. Volatile bits (date, peers, cwd) sit below the cache
        boundary; the static prefix stays byte-stable for prompt caching.
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
        )
        return build_prompt(
            tool_summaries=self.tools.summaries,
            context_files=context_files,
            runtime=runtime,
            has_memory=True,
        )

    def _cwd(self) -> str:
        bash = self.tools.handlers.get("bash")
        # The handler is BashTool.run (bound method); reach its instance's cwd.
        instance = getattr(bash, "__self__", None)
        return getattr(instance, "cwd", "") if instance is not None else ""


def _peer_ids(peers: list[Peer]) -> list[str]:
    return [p.id for p in peers]


def _incoming_text(msg: Message) -> str:
    """Render an incoming message as the user-turn content for the model.

    The sender is named for context, but the loop does not *branch* on it — a
    human and an agent are formatted identically.
    """
    if msg.kind == "tick":
        return "<tick> You're awake. Look for useful work, or sleep (reply empty) if there's nothing to do."
    return f"[message from {msg.sender}]\n{msg.content}"


async def deliver(reply: Message, peers: list[Peer]) -> bool:
    """Route a reply to the peer named by ``reply.recipient``.

    Returns True if delivered. An empty reply (a "sleep") is never delivered.
    A reply to an unknown recipient is dropped with a warning (no peer to send to).
    """
    if not reply.content.strip():
        return False  # sleep — deliver nothing
    for peer in peers:
        if peer.id == reply.recipient:
            await peer.outbound(reply)
            return True
    print(f"[outer_loop] no peer for recipient {reply.recipient!r}; reply dropped")
    return False


async def run(mailbox: Mailbox, peers: list[Peer], agent: Agent) -> None:
    """The forever loop: receive → activate → deliver. Never returns on its own."""
    peer_ids = _peer_ids(peers)
    while True:
        msg = await mailbox.receive()

        # On a tick with a real message already queued behind it, skip the tick
        # entirely and go straight to that message (no point waking up to think
        # when there's concrete work waiting). Otherwise the tick becomes an
        # activation and the agent decides whether to act or "sleep" (reply empty).
        if msg.kind == "tick" and mailbox.has_pending():
            continue

        # Append the incoming message to the durable conversation.
        agent.messages.append({"role": "user", "content": _incoming_text(msg)})
        agent.transcript.append_message("user", _incoming_text(msg))

        system_prompt = agent.build_system_prompt(peer_ids)
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
        await deliver(reply, peers)
        # loop forever; no exit condition
