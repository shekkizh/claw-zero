"""Token estimation — the chars/4 heuristic with a safety margin.

Ported from ``harness/context/token_estimation.py`` with the
``FIXED_IMAGE_TOKENS`` / base64-image path **removed** — claw-zero has no images.
A plain ``len(json.dumps(msg)) / 4`` over the serialized message, scaled by
``SAFETY_MARGIN`` to absorb tokenizer variance.
"""

from __future__ import annotations

import json
from typing import Any

SAFETY_MARGIN = 1.2
"""Multiply raw token estimate by this factor to absorb tokenizer variance."""


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    """Estimate tokens for a single message via the chars/4 heuristic."""
    raw = json.dumps(msg, separators=(",", ":"), default=str)
    return len(raw) // 4


def estimate_messages_tokens(msgs: list[dict[str, Any]]) -> int:
    """Estimate total tokens for a list of messages."""
    return sum(estimate_message_tokens(m) for m in msgs)
