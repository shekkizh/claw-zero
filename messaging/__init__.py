"""Messaging substrate — the equal-operator channel.

The mailbox is the *only* channel claw-zero uses; a human and any future agent
both speak through it. Nothing in the loop branches on who the sender is.
"""

from .mailbox import Mailbox, Message
from .peer import Peer, StdioPeer

__all__ = ["Mailbox", "Message", "Peer", "StdioPeer"]
