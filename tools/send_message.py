"""send_message — reach another participant by name, mid-activation.

claw-zero already delivers your plain-text reply to whoever last addressed you,
so this tool is **not** how you answer the current peer. It is how you reach a
*different* participant — a teammate who didn't just message you, the operator,
or the whole team — without ending your turn. The message lands in the
recipient's inbox and is processed on their next activation; you keep working.

Bound to the shared ``MessageBus`` and the sending agent's id, so an agent can
only send *as itself* and only to participants the bus knows by name. Routing is
the bus's job (``bus.route``); this tool just validates and hands off — exactly
the same delivery path the outer loop uses for a reply.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..messaging.mailbox import Message

if TYPE_CHECKING:
    from ..messaging.bus import MessageBus


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SEND_MESSAGE_DESCRIPTION = """\
Send a message to another participant — a teammate agent or a human operator — \
by their name. Use this to reach someone OTHER than the peer you're currently \
replying to: hand off a subtask, ask a teammate a question, or notify the team \
of something.

This does NOT end your turn. Your final plain-text reply still goes to whoever \
last addressed you; `send_message` is for everyone else. The message is queued \
on the recipient's inbox and processed on their next activation — there is no \
"busy" state and no inbox to poll. Messages addressed to you arrive \
automatically as future activations (shown as `[message from <name>]`); to reply \
to one, address that sender by name.

Addressing:
- `to`: the recipient's name. The names you can reach are listed under Runtime \
context (every participant — agents and the operator — has one).
- `to: "*"`: broadcast to every other agent (not the operator). Linear in team \
size — use only when everyone genuinely needs it.

You cannot send to yourself, and an unknown name is rejected (you'll get the \
list of valid names back)."""


class SendMessageTool:
    """Route a message from this agent to another bus participant."""

    name = "send_message"

    def __init__(self, bus: "MessageBus", sender_id: str) -> None:
        self._bus = bus
        self._sender_id = sender_id

    @property
    def description(self) -> str:
        return SEND_MESSAGE_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "Recipient name (a teammate or the operator), or '*' to "
                        "broadcast to all teammates. Reachable names are under Runtime context."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The message body (plain text).",
                },
            },
            "required": ["to", "content"],
        }

    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        to = params.get("to")
        content = params.get("content")
        if not isinstance(to, str) or not to.strip():
            return {"success": False, "error": "'to' must be a non-empty recipient name (or '*')."}
        if not isinstance(content, str) or not content.strip():
            return {"success": False, "error": "'content' must be a non-empty string."}

        reachable = self._bus.reachable_from(self._sender_id)

        if to == "*":
            # Broadcast to every other agent — not external participants (the operator).
            targets = [a for a in self._bus.agent_ids if a != self._sender_id]
            if not targets:
                return {"success": False, "error": "No teammates to broadcast to."}
        else:
            if to == self._sender_id:
                return {"success": False, "error": "Cannot send a message to yourself."}
            if to not in reachable:
                return {
                    "success": False,
                    "error": f"Unknown recipient {to!r}.",
                    "reachable": reachable,
                }
            targets = [to]

        delivered: list[str] = []
        for target in targets:
            ok = await self._bus.route(
                Message(
                    sender=self._sender_id,
                    recipient=target,
                    content=content,
                    kind="message",
                    ts=_now_iso(),
                )
            )
            if ok:
                delivered.append(target)

        return {"success": True, "delivered_to": delivered}
