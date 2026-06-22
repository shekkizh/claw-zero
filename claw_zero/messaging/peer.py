"""Peer — a unit/operator the agent can talk to, and the stdio implementation.

A ``Peer`` is the transport abstraction over one correspondent. The human is
just another peer (``StdioPeer``); a future agent-to-agent transport implements
the same interface. The loop never asks "is this the human?" — it routes a
reply by ``recipient`` to whichever peer owns that id.

``inbound`` pumps external input *into* the mailbox; ``outbound`` delivers one of
the agent's messages *out*. A self-tick source (``tick_source``) is just a
coroutine that periodically enqueues a ``kind="tick"`` message — it is not a peer
(ticks come from "self", and nobody delivers replies to a tick).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from .mailbox import Mailbox, Message


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@runtime_checkable
class Peer(Protocol):
    """A correspondent the agent exchanges messages with.

    Implementations bridge some external channel (stdio now; an A2A transport
    later) to the in-memory mailbox.
    """

    id: str

    async def inbound(self, mailbox: Mailbox) -> None:
        """Read from the external channel and ``mailbox.send(...)`` each message.

        Runs as a long-lived background task; returns only when the channel
        closes (e.g. stdin EOF).
        """
        ...

    async def outbound(self, msg: Message) -> None:
        """Deliver one of the agent's messages out over the external channel."""
        ...


class StdioPeer:
    """The human, as just another peer, over stdin/stdout.

    - ``inbound``: reads lines from stdin, wrapping each as a
      ``Message(sender=self.id, kind="message")``.
    - ``outbound``: prints ``msg.content`` to stdout, prefixed with the agent id
      for clarity.

    Reads run in a thread executor so the blocking ``input()`` does not stall the
    event loop (keeping ticks and any agent peers responsive).
    """

    def __init__(self, peer_id: str = "human", agent_id: str = "claw-zero") -> None:
        self.id = peer_id
        self._agent_id = agent_id

    async def inbound(self, mailbox: Mailbox) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except (EOFError, RuntimeError):
                return
            if line == "":  # EOF — stdin closed
                return
            text = line.rstrip("\n")
            if not text.strip():
                continue
            await mailbox.send(
                Message(
                    sender=self.id,
                    recipient=self._agent_id,
                    content=text,
                    kind="message",
                    ts=_now_iso(),
                )
            )

    async def outbound(self, msg: Message) -> None:
        # Prefix with the agent id so a human can tell who is speaking.
        sys.stdout.write(f"[{msg.sender}] {msg.content}\n")
        sys.stdout.flush()


async def tick_source(
    mailbox: Mailbox,
    interval_seconds: float,
    *,
    agent_id: str = "claw-zero",
) -> None:
    """Periodically enqueue a ``kind="tick"`` message (the pacing pattern).

    A tick is "you're awake, what now?" — the loop may find useful work or
    "sleep" (return without sending). This coroutine runs forever; cancel it to
    stop. Kept off by default behind a config flag (``tick_seconds``).
    """
    while True:
        await asyncio.sleep(interval_seconds)
        await mailbox.send(
            Message(
                sender="self",
                recipient=agent_id,
                content="",
                kind="tick",
                ts=_now_iso(),
            )
        )


# ---------------------------------------------------------------------------
# Manual demo:  python -m claw_zero.messaging.peer
#   Type a line on stdin → it is received as a Message and echoed via outbound.
# ---------------------------------------------------------------------------

async def _demo() -> None:  # pragma: no cover - interactive
    mailbox = Mailbox()
    peer = StdioPeer()

    async def _pump() -> None:
        await peer.inbound(mailbox)
        # Signal end-of-input by enqueuing a sentinel tick.
        await mailbox.send(Message(sender="self", recipient=peer.id, content="", kind="tick"))

    pump_task = asyncio.create_task(_pump())
    print("claw-zero peer demo — type a line, Ctrl-D to exit.")
    while True:
        msg = await mailbox.receive()
        if msg.kind == "tick":
            break
        print(f"  received: Message(sender={msg.sender!r}, kind={msg.kind!r}, content={msg.content!r})")
        await peer.outbound(
            Message(sender="claw-zero", recipient=msg.sender, content=f"echo: {msg.content}")
        )
    await pump_task


if __name__ == "__main__":  # pragma: no cover - interactive
    try:
        asyncio.run(_demo())
    except KeyboardInterrupt:
        pass
