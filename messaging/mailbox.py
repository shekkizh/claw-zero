"""Mailbox — one agent's in-memory inbox.

Ports the *shape* of Claude Code's mailbox
(`claude-code/utils/mailbox.ts`: send / receive / poll) onto an
``asyncio.Queue`` (ALE Claw already uses ``asyncio.Queue`` for subagent
results — same idea). Each agent owns one of these; a human peer, another agent,
and the self-tick source all enqueue ``Message`` objects here, and the agent
dequeues them uniformly. Nothing downstream branches on *who* the sender is.

This is the substrate of claw-zero's agent-to-agent messaging: agents exchange
``Message`` objects through their mailboxes (routed by ``MessageBus``), so an
agent talking to another agent and an agent talking to the human are the same
operation. Everything is in-process — there is no network layer, by design.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field


def _new_message_id() -> str:
    """A fresh opaque message id. Not derived from time, so it is cache-safe."""
    return f"msg-{uuid.uuid4().hex[:12]}"


@dataclass
class Message:
    """A single message moving through the mailbox.

    The operator and an agent produce structurally identical messages — the only
    field the loop is allowed to branch on is ``kind`` (``"tick"`` vs
    ``"message"``), never ``sender``.

    Attributes:
        sender: Name of the originator — an agent's id, the operator's name, or
            ``"self"`` for a tick. **Not special-cased** anywhere in the loop.
        recipient: The participant name this message is addressed to.
        content: The message body (plain text).
        kind: ``"message"`` | ``"tick"``. Room to grow; treat uniformly except
            for the one allowed tick/message branch.
        id: Opaque message id (auto-generated when omitted).
        ts: ISO-8601 timestamp string. Passed in by the producer; the hot paths
            never call ``datetime.now()`` themselves (keeps cached prefixes
            byte-stable and keeps the dataclass deterministic for tests).
    """

    sender: str
    recipient: str
    content: str
    kind: str = "message"
    id: str = field(default_factory=_new_message_id)
    ts: str = ""


class Mailbox:
    """A FIFO message queue backed by ``asyncio.Queue``.

    ``send`` enqueues, ``receive`` awaits the next message (blocking when
    empty), and ``poll`` returns the next message without blocking (``None`` when
    empty). FIFO order is guaranteed by the underlying queue.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Message] = asyncio.Queue()

    async def send(self, msg: Message) -> None:
        """Enqueue a message. Never blocks (the queue is unbounded)."""
        await self._queue.put(msg)

    async def receive(self) -> Message:
        """Await and return the next message in FIFO order (blocks if empty)."""
        return await self._queue.get()

    def poll(self) -> Message | None:
        """Return the next message without blocking, or ``None`` if empty."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def has_pending(self) -> bool:
        """True if at least one message is waiting (non-destructive peek)."""
        return self._queue.qsize() > 0

    def __len__(self) -> int:
        """Number of messages currently waiting (mirrors mailbox.ts ``length``)."""
        return self._queue.qsize()
