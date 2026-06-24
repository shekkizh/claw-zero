"""MessageBus — the routing layer for a flat peer mesh.

claw-zero started as a single agent with one ``Mailbox`` and a list of external
``Peer`` s. A *team* is just more of the same units: several agents, each with
its own mailbox, plus the external peers (the human over stdio). The bus is the
one place that knows the whole roster and routes a ``Message`` to wherever its
``recipient`` lives — an agent's inbox or an external peer's ``outbound()``.

The design stays true to the original thesis: **humans and agents are equal
operators.** Nothing here branches on *who* the sender is. Routing is purely by
``recipient`` id; an agent peer and a human peer are addressed identically. A
single-agent run is the degenerate case — one agent on the bus, one external
peer — so the bus subsumes the old ``[StdioPeer]`` list without special-casing.

The bus IS claw-zero's agent-to-agent layer: agents reach each other by sending
``Message`` objects that the bus routes to the recipient's inbox — the same path
a reply to the human takes. Everything is in-process (coroutines in one event
loop); there is no network transport, by design.

What the bus owns:
  - ``inboxes``: ``agent_id -> Mailbox``. Each agent's loop awaits its own.
  - ``peers``: ``peer_id -> Peer``. External I/O channels (the human over stdio).
  - ``route``: deliver one message to the matching inbox or external peer.
"""

from __future__ import annotations

from .mailbox import Mailbox, Message
from .peer import Peer


class MessageBus:
    """Routes messages across in-process agent inboxes and external peers.

    Agents are registered with ``add_agent`` (creating their inbox); external
    correspondents (the human) with ``add_peer``. ``route`` is the single
    delivery point used by the outer loop's ``deliver`` and by the
    ``send_message`` tool — both just hand a ``Message`` to the bus and let it
    find the recipient.
    """

    def __init__(self) -> None:
        self._inboxes: dict[str, Mailbox] = {}
        self._peers: dict[str, Peer] = {}

    # -- registration --------------------------------------------------------

    def add_agent(self, agent_id: str) -> Mailbox:
        """Register an agent and return (creating if needed) its inbox.

        Idempotent: re-registering an existing agent returns the same mailbox so
        a roster entry and a runtime spawn of the same id don't split its inbox.
        """
        if agent_id not in self._inboxes:
            self._inboxes[agent_id] = Mailbox()
        return self._inboxes[agent_id]

    def add_peer(self, peer: Peer) -> None:
        """Register an external peer (e.g. the human over stdio) by its id."""
        self._peers[peer.id] = peer

    # -- introspection -------------------------------------------------------

    def inbox(self, agent_id: str) -> Mailbox:
        """Return the inbox for ``agent_id`` (KeyError if not registered)."""
        return self._inboxes[agent_id]

    @property
    def agent_ids(self) -> list[str]:
        """All registered agent ids, in registration order."""
        return list(self._inboxes)

    @property
    def peer_ids(self) -> list[str]:
        """All registered external peer ids."""
        return list(self._peers)

    def knows(self, recipient: str) -> bool:
        """True if ``recipient`` is a routable agent or external peer."""
        return recipient in self._inboxes or recipient in self._peers

    def reachable_from(self, agent_id: str) -> list[str]:
        """Ids ``agent_id`` may address: every other agent + every external peer.

        Surfaced in the prompt's Runtime context so an agent knows its teammates
        by name. The agent never lists itself.
        """
        others = [a for a in self._inboxes if a != agent_id]
        return others + list(self._peers)

    def has_pending(self) -> bool:
        """True if any agent inbox still has a message waiting (drain check)."""
        return any(mb.has_pending() for mb in self._inboxes.values())

    # -- routing -------------------------------------------------------------

    async def route(self, msg: Message) -> bool:
        """Deliver ``msg`` to its recipient. Returns True if delivered.

        An agent recipient → enqueue on that agent's inbox. An external peer →
        its ``outbound()``. Unknown recipient → dropped with a warning (there is
        nowhere to put it). Routing never branches on the sender.
        """
        if msg.recipient in self._inboxes:
            await self._inboxes[msg.recipient].send(msg)
            return True
        peer = self._peers.get(msg.recipient)
        if peer is not None:
            await peer.outbound(msg)
            return True
        print(f"[bus] no recipient {msg.recipient!r}; message from {msg.sender!r} dropped")
        return False
