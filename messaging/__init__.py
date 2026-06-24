"""Messaging substrate — the equal-operator channel.

The ``MessageBus`` routes ``Message`` objects between per-agent mailboxes and the
human's stdio channel; every participant is addressed by name, and nothing
branches on who the sender is. This in-process message-passing is claw-zero's
agent-to-agent layer.
"""

from .bus import MessageBus
from .mailbox import Mailbox, Message
from .peer import Peer, StdioPeer

__all__ = ["MessageBus", "Mailbox", "Message", "Peer", "StdioPeer"]
