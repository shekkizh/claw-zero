"""Context management — token estimation, in-place compaction, transcript.

Ported from the ALE Claw harness ``context/`` package with all image-token
logic stripped (claw-zero has no images). Operates on OpenAI chat-shaped
messages (``assistant.tool_calls`` + ``role="tool"`` results), not the CUA
canonical block format.
"""

from .token_estimation import estimate_message_tokens, estimate_messages_tokens
from .compaction import CompactionResult, compact_messages
from .transcript import Transcript

__all__ = [
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "CompactionResult",
    "compact_messages",
    "Transcript",
]
