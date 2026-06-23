"""Memory flush — the pre-compaction durable-memory turn.

Ports ``harness/memory/memory_flush.py`` + ``memory_flush_policy.py``, adapted to
claw-zero's ``llm.call`` (chat shape) and the new ``MemoryStore`` method names.
Before old context is summarized away, the model gets one turn to persist durable
memories: it calls ``memory_write(content, target)`` (routed to the store), or
replies with the silent token when there's nothing worth saving.

Two independent triggers fire a flush (mirroring OpenClaw), guarded by a dedup so
at most one flush runs per compaction cycle:
  1. token count ≥ ``compaction_trigger − reserve − soft_threshold``
  2. transcript file ≥ ``force_transcript_bytes`` (2 MB)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import MemoryStore


SILENT_REPLY_TOKEN = "[!silent]"

DEFAULT_COMPACTION_RATIO = 0.80
SOFT_THRESHOLD_TOKENS = 4000
RESERVE_TOKENS_FLOOR = 20_000
FORCE_TRANSCRIPT_BYTES = 2 * 1024 * 1024  # 2 MB; set 0 to disable

FLUSH_SYSTEM_PROMPT = (
    "Pre-compaction memory flush turn. The activation is near auto-compaction; "
    f"capture durable memories to disk now. You may reply, but usually "
    f"{SILENT_REPLY_TOKEN} is correct."
)

FLUSH_PROMPT = (
    "Pre-compaction memory flush. Store durable memories now: use memory_write "
    "with target='session' for raw observations, or target='curated' for "
    "strategies and insights worth keeping across activations. Only store what "
    "would be valuable later — key decisions, progress, discovered patterns, "
    f"important state. If nothing to store, reply with {SILENT_REPLY_TOKEN}."
)


@dataclass
class FlushState:
    """Per-run flush bookkeeping (the loop owns one of these).

    ``compaction_count`` increments each time the loop compacts; the dedup guard
    compares it against the count recorded at the last flush so a single
    compaction cycle never flushes twice.
    """

    compaction_count: int = 0
    flushed_at_compaction_count: int | None = None


def has_already_flushed(state: FlushState) -> bool:
    """True if a flush already ran in the current compaction cycle."""
    return (
        state.flushed_at_compaction_count is not None
        and state.flushed_at_compaction_count == state.compaction_count
    )


def should_run_memory_flush(
    state: FlushState,
    *,
    current_tokens: int,
    context_window: int,
    transcript_bytes: int = 0,
    compaction_ratio: float = DEFAULT_COMPACTION_RATIO,
    soft_threshold_tokens: int = SOFT_THRESHOLD_TOKENS,
    reserve_tokens: int = RESERVE_TOKENS_FLOOR,
    force_transcript_bytes: int = FORCE_TRANSCRIPT_BYTES,
) -> bool:
    """Decide whether a pre-compaction flush should run (token OR transcript trigger)."""
    by_tokens = False
    if current_tokens > 0 and 0 < compaction_ratio <= 1:
        trigger = int(context_window * compaction_ratio)
        threshold = max(0, trigger - reserve_tokens - soft_threshold_tokens)
        by_tokens = threshold > 0 and current_tokens >= threshold

    by_transcript = force_transcript_bytes > 0 and transcript_bytes >= force_transcript_bytes

    if not (by_tokens or by_transcript):
        return False
    return not has_already_flushed(state)


# memory_write tool schema offered only during the flush turn.
_MEMORY_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_write",
        "description": (
            "Write durable memory. target='session' appends to the session log; "
            "target='curated' overwrites AGENT_MEMORY.md (include everything worth keeping)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The text to persist."},
                "target": {
                    "type": "string",
                    "enum": ["session", "curated"],
                    "description": "Where to write: 'session' (append) or 'curated' (overwrite).",
                },
            },
            "required": ["content"],
        },
    },
}


def _serialize_history(messages: list[dict[str, Any]]) -> str:
    """Flatten chat-shape messages to readable text for the flush model.

    The flush turn can't replay raw tool_call/tool result pairing (it's a
    side turn with its own tiny message list), so we serialize the conversation
    as plain text — role-prefixed, with tool calls and results summarized.
    """
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            chunks = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            text = " ".join(c for c in chunks if c).strip()
        seg = f"[{role}] {text}" if text else ""
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            args = fn.get("arguments", "")
            if isinstance(args, str) and len(args) > 200:
                args = args[:200] + "..."
            seg = (seg + f" [tool_call: {fn.get('name', '?')}({args})]").strip()
        if msg.get("role") == "tool":
            tool_out = content if isinstance(content, str) else str(content)
            if len(tool_out) > 200:
                tool_out = tool_out[:200] + "..."
            seg = f"[tool_result: {tool_out}]"
        if seg:
            parts.append(seg)
    return "\n".join(parts)


async def run_memory_flush(
    *,
    model: str,
    messages: list[dict[str, Any]],
    memory_store: "MemoryStore",
    state: FlushState,
) -> bool:
    """Run one pre-compaction flush turn via ``llm.call``.

    Routes any ``memory_write`` calls to the store. Records the flush against the
    current compaction cycle even on failure (prevents retry loops). Returns True
    if the flush ran (regardless of whether anything was written).
    """
    from .. import llm

    conversation = _serialize_history(messages)
    user_content = (
        f"<conversation>\n{conversation}\n</conversation>\n\n{FLUSH_PROMPT}"
        if conversation
        else FLUSH_PROMPT
    )

    try:
        result = await llm.call(
            model,
            [{"role": "user", "content": user_content}],
            system=FLUSH_SYSTEM_PROMPT,
            tools=[_MEMORY_WRITE_TOOL],
            max_tokens=4096,
        )
        for tc in result.tool_calls:
            if tc.name != "memory_write":
                continue
            try:
                args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else (tc.arguments or {})
            except json.JSONDecodeError:
                # Truncated/malformed JSON args — skip this write, don't crash the flush.
                continue
            content = (args.get("content") or "").strip()
            if not content:
                continue
            if args.get("target") == "curated":
                memory_store.write_curated(content)
            else:
                try:
                    memory_store.append_session(content)
                except RuntimeError:
                    # No session initialized — fall back to curated so we don't lose it.
                    memory_store.write_curated(content)
    except Exception as exc:  # non-fatal: a failed flush must not break the loop
        print(f"[MemoryFlush] failed (non-fatal): {exc}")
    finally:
        state.flushed_at_compaction_count = state.compaction_count
    return True


async def maybe_flush_memory(
    *,
    model: str,
    messages: list[dict[str, Any]],
    memory_store: "MemoryStore",
    state: FlushState,
    current_tokens: int,
    context_window: int,
    transcript_bytes: int = 0,
    compaction_ratio: float = DEFAULT_COMPACTION_RATIO,
) -> bool:
    """Run a flush iff the triggers say so. Returns True if a flush ran."""
    if not should_run_memory_flush(
        state,
        current_tokens=current_tokens,
        context_window=context_window,
        transcript_bytes=transcript_bytes,
        compaction_ratio=compaction_ratio,
    ):
        return False
    return await run_memory_flush(
        model=model, messages=messages, memory_store=memory_store, state=state
    )
