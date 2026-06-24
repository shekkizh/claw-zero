"""Peer — an external I/O channel into the bus, plus stdio + ticks.

A ``Peer`` bridges a non-agent I/O channel into the bus. The only one is the
human's terminal (``StdioPeer``) — ``Peer`` exists so the human can be addressed
by id exactly like an agent, not as a hook for external/remote agents (there are
none; the team is in-process). In-process agents are **not** peers — they own
inboxes on the ``MessageBus`` and run their own loops. The bus never asks "is
this the human?": a message is routed by ``recipient`` to whichever participant
owns that id (see ``messaging/bus.py``).

``inbound`` pumps external input *into* the bus (routing each message to its
addressed recipient); ``outbound`` delivers one of an agent's messages *out*. A
self-tick source (``tick_source``) periodically enqueues ``kind="tick"`` messages
for each agent — it is not a peer (ticks come from "self"; nobody replies to one).
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .mailbox import Message

if TYPE_CHECKING:  # avoid a bus<->peer import cycle (annotations are strings)
    from .bus import MessageBus


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# "@coder rest" or "coder: rest" — an explicit recipient prefix the operator can
# use to address one agent by name. Without a prefix, the default recipient gets it.
_ADDRESS_RE = re.compile(r"^\s*(?:@(?P<at>[\w.-]+)\s+|(?P<colon>[\w.-]+):\s+)(?P<body>.*)$", re.DOTALL)


@runtime_checkable
class Peer(Protocol):
    """A non-agent I/O channel the team exchanges messages with.

    The one implementation is ``StdioPeer`` (the human's terminal). Agents are
    not peers — they are bus participants with their own inboxes.
    """

    id: str

    async def inbound(self, bus: "MessageBus") -> None:
        """Read the external channel and ``bus.route(...)`` each message.

        Runs as a long-lived background task; returns only when the channel
        closes (e.g. stdin EOF).
        """
        ...

    async def outbound(self, msg: Message) -> None:
        """Deliver one of an agent's messages out over the external channel."""
        ...


def parse_address(text: str, default_recipient: str, known: list[str]) -> tuple[str, str]:
    """Split an optional ``@id``/``id:`` recipient prefix off a typed line.

    Returns ``(recipient, body)``. The prefix is honored only when it names a
    *known* participant; otherwise the whole line is treated as content for the
    default recipient (so a stray colon in normal prose isn't mis-read as
    addressing). The default recipient is used when there is no valid prefix.
    """
    m = _ADDRESS_RE.match(text)
    if m:
        target = m.group("at") or m.group("colon")
        if target in known:
            return target, m.group("body").strip()
    return default_recipient, text


class StdioPeer:
    """The human operator, as a named participant, over stdin/stdout.

    The operator has a name (``peer_id``) and is addressed by it like any agent.

    - ``inbound``: reads lines from stdin. Each line may address one agent with
      an ``@name`` / ``name:`` prefix (resolved against the bus roster); otherwise
      it goes to ``default_recipient``. The result is routed through the bus.
    - ``outbound``: prints ``msg.content`` to stdout, prefixed with the sender
      name so the operator can tell which participant is speaking.

    Reads run in a thread executor so the blocking ``input()`` does not stall the
    event loop (keeping ticks and agent loops responsive).
    """

    def __init__(self, peer_id: str = "operator", default_recipient: str = "claw-zero") -> None:
        self.id = peer_id
        self.default_recipient = default_recipient

    async def inbound(self, bus: "MessageBus") -> None:
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
            recipient, body = parse_address(text, self.default_recipient, bus.agent_ids)
            if not body.strip():
                continue
            await bus.route(
                Message(
                    sender=self.id,
                    recipient=recipient,
                    content=body,
                    kind="message",
                    ts=_now_iso(),
                )
            )

    async def outbound(self, msg: Message) -> None:
        # Prefix with the sender name so the operator can tell who is speaking.
        sys.stdout.write(f"[{msg.sender}] {msg.content}\n")
        sys.stdout.flush()


async def tick_source(
    bus: "MessageBus",
    interval_seconds: float,
    *,
    agent_ids: list[str] | None = None,
) -> None:
    """Periodically enqueue a ``kind="tick"`` message for each agent (pacing).

    A tick is "you're awake, what now?" — an agent may find useful work or
    "sleep" (return without sending). When ``agent_ids`` is None the whole
    current roster is ticked. This coroutine runs forever; cancel it to stop.
    Kept off by default behind a config flag (``tick_seconds``).
    """
    while True:
        await asyncio.sleep(interval_seconds)
        targets = agent_ids if agent_ids is not None else bus.agent_ids
        for agent_id in targets:
            await bus.route(
                Message(
                    sender="self",
                    recipient=agent_id,
                    content="",
                    kind="tick",
                    ts=_now_iso(),
                )
            )
