"""Context compaction — budget-aware history pruning + LLM summarization."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .token_estimation import (
    SAFETY_MARGIN,
    estimate_message_tokens,
    estimate_messages_tokens,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_CHUNK_RATIO = 0.4
MIN_CHUNK_RATIO = 0.15
DEFAULT_MAX_HISTORY_SHARE = 0.6
"""Post-compaction raw history target as a share of the context window.

This is intentionally separate from ``BASE_CHUNK_RATIO``: chunk ratio controls
summarizer request size, while history share controls how much recent raw state
survives after compaction.
"""
SUMMARIZATION_OVERHEAD_TOKENS = 4096
DEFAULT_SUMMARY_FALLBACK = "No prior history."
MAX_SUMMARIZATION_RETRIES = 3
SUMMARIZATION_TIMEOUT = 120
MAX_RECENT_TURNS_PRESERVE = 12

_SKIP_SYNTHESIS_STOP_REASONS = frozenset({"error", "aborted"})

SYNTHETIC_TOOL_RESULT_CONTENT = (
    "[compaction] missing tool result — synthetic error result for transcript repair."
)

IDENTIFIER_PRESERVATION_INSTRUCTIONS = (
    "Preserve all opaque identifiers exactly as written (no shortening or "
    "reconstruction), including UUIDs, hashes, IDs, tokens, API keys, "
    "hostnames, IPs, ports, URLs, and file names."
)

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Read a conversation between an "
    "agent and its peers, then produce a structured summary in the exact format "
    "specified. Do NOT continue the conversation. Do NOT answer any questions in "
    "it. ONLY output the structured summary.\n\n"
    + IDENTIFIER_PRESERVATION_INSTRUCTIONS
)

SUMMARIZATION_PROMPT = """\
The messages above are a conversation to summarize. Create a structured context \
checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is being accomplished? Can be multiple items.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned]
- [Or "(none)"]

## Progress
### Done
- [x] [Completed work]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, identifiers, or references needed to continue]
- [Or "(none)"]

Keep each section concise. Preserve exact file paths, identifiers, and error messages.\
"""


# ---------------------------------------------------------------------------
# CompactionResult
# ---------------------------------------------------------------------------

@dataclass
class CompactionResult:
    summary: str
    tokens_before: int
    tokens_after: int
    first_kept_message_index: int
    chunks_processed: int


# ---------------------------------------------------------------------------
# Tool pairing repair  (chat shape)
# ---------------------------------------------------------------------------

@dataclass
class ToolPairingRepairReport:
    messages: list[dict[str, Any]]
    dropped_orphan_count: int
    dropped_duplicate_count: int
    added_synthetic_count: int


def _assistant_call_ids(msg: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for tc in msg.get("tool_calls", []) or []:
        cid = tc.get("id") if isinstance(tc, dict) else None
        if cid:
            ids.append(cid)
    return ids


def repair_tool_use_result_pairing(
    messages: list[dict[str, Any]],
) -> ToolPairingRepairReport:
    """Repair orphaned assistant.tool_calls / tool-result pairs (chat shape).

    1. Drop tool-result messages whose ``tool_call_id`` matches no assistant call.
    2. Drop duplicate tool-results (same id seen before).
    3. Insert a synthetic error tool-result for any assistant call left without a
       result (unless the assistant turn's stop reason is error/aborted).

    A tool-result message here is ``{"role": "tool", "tool_call_id": <id>,
    "content": ...}`` (one result per message — the chat convention).
    """
    if not messages:
        return ToolPairingRepairReport([], 0, 0, 0)

    valid_call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            valid_call_ids.update(_assistant_call_ids(msg))

    dropped_orphan = 0
    dropped_dup = 0
    seen_result_ids: set[str] = set()
    matched: set[str] = set()

    # Pass 1: filter tool-result messages.
    filtered: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "tool":
            rid = msg.get("tool_call_id", "")
            if rid and rid not in valid_call_ids:
                dropped_orphan += 1
                continue
            if rid in seen_result_ids:
                dropped_dup += 1
                continue
            if rid:
                seen_result_ids.add(rid)
                matched.add(rid)
            filtered.append(msg)
        else:
            filtered.append(msg)

    # Pass 2: insert synthetic results for unmatched assistant calls, right after
    # the assistant message that issued them.
    added_synthetic = 0
    final: list[dict[str, Any]] = []
    for msg in filtered:
        final.append(msg)
        if msg.get("role") != "assistant":
            continue
        stop_reason = msg.get("stop_reason")
        for cid in _assistant_call_ids(msg):
            if cid in matched:
                continue
            if stop_reason in _SKIP_SYNTHESIS_STOP_REASONS:
                continue
            final.append({
                "role": "tool",
                "tool_call_id": cid,
                "content": SYNTHETIC_TOOL_RESULT_CONTENT,
            })
            matched.add(cid)
            added_synthetic += 1

    return ToolPairingRepairReport(final, dropped_orphan, dropped_dup, added_synthetic)


# ---------------------------------------------------------------------------
# Recent-turn preservation
# ---------------------------------------------------------------------------

def split_preserved_recent_turns(
    messages: list[dict[str, Any]],
    preserve_count: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split out the last ``preserve_count`` turns (counted by assistant messages).

    Returns ``(pruneable, preserved)``. Tool pairing is repaired on the pruneable
    portion (the split can orphan a pair at the boundary).
    """
    preserve_count = min(preserve_count, MAX_RECENT_TURNS_PRESERVE)
    if preserve_count <= 0 or not messages:
        return list(messages), []

    assistant_count = 0
    split_index = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            assistant_count += 1
            if assistant_count >= preserve_count:
                split_index = i
                break

    if assistant_count < preserve_count:
        return [], list(messages)

    pruneable = messages[:split_index]
    preserved = messages[split_index:]
    if pruneable:
        pruneable = repair_tool_use_result_pairing(pruneable).messages
    return pruneable, preserved


# ---------------------------------------------------------------------------
# Chunk splitting
# ---------------------------------------------------------------------------

def chunk_messages_by_token_share(
    messages: list[dict[str, Any]], parts: int = 2
) -> list[list[dict[str, Any]]]:
    """Split messages into ``parts`` chunks targeting equal token budgets."""
    if not messages or parts < 1:
        return []
    if parts == 1:
        return [list(messages)]

    total = estimate_messages_tokens(messages)
    if total == 0:
        return [list(messages)]

    target = total / parts
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for msg in messages:
        mt = estimate_message_tokens(msg)
        if current and current_tokens + mt > target and len(chunks) < parts - 1:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(msg)
        current_tokens += mt
    if current:
        chunks.append(current)
    return chunks


def chunk_messages_by_max_tokens(
    messages: list[dict[str, Any]], max_tokens: int
) -> list[list[dict[str, Any]]]:
    """Split messages so each chunk stays under ``max_tokens`` (safety-scaled)."""
    if not messages or max_tokens <= 0:
        return []
    safe_max = int(max_tokens / SAFETY_MARGIN)
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for msg in messages:
        mt = estimate_message_tokens(msg)
        if current and current_tokens + mt > safe_max:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(msg)
        current_tokens += mt
    if current:
        chunks.append(current)
    return chunks


def compute_adaptive_chunk_ratio(
    messages: list[dict[str, Any]], context_window: int
) -> float:
    """Shrink the chunk ratio when messages are large relative to the window."""
    if not messages or context_window <= 0:
        return BASE_CHUNK_RATIO
    avg = estimate_messages_tokens(messages) / len(messages)
    ratio = (avg * SAFETY_MARGIN) / context_window
    if ratio > 0.1:
        reduction = min(ratio * 2, BASE_CHUNK_RATIO - MIN_CHUNK_RATIO)
        return max(MIN_CHUNK_RATIO, BASE_CHUNK_RATIO - reduction)
    return BASE_CHUNK_RATIO


# ---------------------------------------------------------------------------
# Serialization for the summary prompt  (chat shape)
# ---------------------------------------------------------------------------

def serialize_messages_for_summary(messages: list[dict[str, Any]]) -> str:
    """Convert chat-shape messages to readable text for the summarizer."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        parts: list[str] = []
        if isinstance(content, str) and content:
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            args = fn.get("arguments", "")
            if isinstance(args, str) and len(args) > 200:
                args = args[:200] + "..."
            parts.append(f"[tool_call: {fn.get('name', '?')}({args})]")
        if role == "tool":
            res = content if isinstance(content, str) else str(content)
            parts = [f"[tool_result: {res[:500]}]"]
        if parts:
            lines.append(f"[{role}] {' | '.join(parts)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM summarization
# ---------------------------------------------------------------------------

async def summarize_chunk(
    messages: list[dict[str, Any]],
    model: str,
    *,
    previous_summary: str | None = None,
    timeout: int = SUMMARIZATION_TIMEOUT,
) -> str:
    """Summarize one chunk via ``llm.call`` with retry/backoff."""
    from .. import llm

    conversation = serialize_messages_for_summary(messages)
    user_parts: list[str] = []
    if previous_summary:
        user_parts.append(f"<previous-summary>\n{previous_summary}\n</previous-summary>\n")
    user_parts.append(f"<conversation>\n{conversation}\n</conversation>\n")
    user_parts.append(SUMMARIZATION_PROMPT)

    last_error: Exception | None = None
    for attempt in range(MAX_SUMMARIZATION_RETRIES):
        try:
            result = await llm.call(
                model,
                [{"role": "user", "content": "\n".join(user_parts)}],
                system=SUMMARIZATION_SYSTEM_PROMPT,
                max_tokens=2048,
                timeout=timeout,
            )
            return result.text or DEFAULT_SUMMARY_FALLBACK
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < MAX_SUMMARIZATION_RETRIES - 1:
                await asyncio.sleep(min(0.5 * (2 ** attempt), 5.0))
    raise last_error  # type: ignore[misc]


async def summarize_with_fallback(
    messages: list[dict[str, Any]],
    model: str,
    max_chunk_tokens: int,
    *,
    timeout: int = SUMMARIZATION_TIMEOUT,
) -> str:
    """Summarize all messages, chunked; static fallback if the LLM path fails."""
    try:
        chunks = chunk_messages_by_max_tokens(messages, max_chunk_tokens)
        summary: str | None = None
        for chunk in chunks:
            summary = await summarize_chunk(chunk, model, previous_summary=summary, timeout=timeout)
        if summary:
            return summary
    except Exception as exc:  # noqa: BLE001
        print(f"[Compaction] summarization failed, using static fallback: {exc}")
    return (
        f"[Compaction fallback] {len(messages)} earlier messages could not be "
        "summarized; they contained tool calls and text exchanges."
    )


# ---------------------------------------------------------------------------
# Budgeting helpers
# ---------------------------------------------------------------------------

def _compute_kept_budget(
    context_window: int,
    max_history_share: float,
    instructions_tokens: int,
    preserved_tokens: int,
) -> int:
    available = (
        int(context_window * max_history_share)
        - instructions_tokens
        - SUMMARIZATION_OVERHEAD_TOKENS
        - preserved_tokens
    )
    return max(available, 2000)


def _split_compact_and_keep(
    pruneable: list[dict[str, Any]],
    available_for_kept: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Half-split pruneable, then prune the kept side until it fits the budget."""
    if not pruneable:
        return [], []
    halves = chunk_messages_by_token_share(pruneable, parts=2)
    if len(halves) < 2:
        return pruneable, []
    to_compact, to_keep = halves[0], halves[1]
    while to_keep and estimate_messages_tokens(to_keep) > available_for_kept:
        sub = chunk_messages_by_token_share(to_keep, parts=2)
        if len(sub) < 2:
            break
        to_compact = to_compact + sub[0]
        to_keep = repair_tool_use_result_pairing(sub[1]).messages
    return to_compact, to_keep


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def compact_messages(
    messages: list[dict[str, Any]],
    model: str,
    context_window: int,
    *,
    instructions_tokens: int = 0,
    max_history_share: float = DEFAULT_MAX_HISTORY_SHARE,
    recent_turns_preserve: int = 3,
    timeout: int = SUMMARIZATION_TIMEOUT,
) -> CompactionResult:
    """Compact older messages into an LLM summary, preserving recent turns.

    Args:
        messages: Full chat-shape history to compact.
        model: OpenAI model id for the summarization call.
        context_window: Context window size in tokens.
        instructions_tokens: Estimated system-prompt token count (subtracted from
            the kept budget).
        max_history_share: Share of the window budgeted for kept history.
        recent_turns_preserve: Recent turns (assistant messages) never summarized.

    Returns:
        ``CompactionResult`` with the summary, token counts, and the index in the
        original list where the kept tail begins.
    """
    if not messages:
        return CompactionResult(DEFAULT_SUMMARY_FALLBACK, 0, 0, 0, 0)

    tokens_before = estimate_messages_tokens(messages)

    pruneable, preserved = split_preserved_recent_turns(messages, recent_turns_preserve)
    preserved_tokens = estimate_messages_tokens(preserved) if preserved else 0
    available_for_kept = _compute_kept_budget(
        context_window, max_history_share, instructions_tokens, preserved_tokens
    )

    # If everything is preserved but the preserved set alone exceeds budget,
    # move its older half into pruneable so we still shrink.
    if (
        not pruneable
        and preserved
        and preserved_tokens > available_for_kept + SUMMARIZATION_OVERHEAD_TOKENS
    ):
        halves = chunk_messages_by_token_share(preserved, parts=2)
        if len(halves) >= 2:
            pruneable, preserved = halves[0], halves[1]
            preserved_tokens = estimate_messages_tokens(preserved)
            available_for_kept = _compute_kept_budget(
                context_window, max_history_share, instructions_tokens, preserved_tokens
            )

    to_compact, to_keep = _split_compact_and_keep(pruneable, available_for_kept)

    if to_keep:
        to_keep = repair_tool_use_result_pairing(to_keep).messages
    final_kept = to_keep + preserved
    first_kept_index = len(messages) - len(final_kept)

    chunk_ratio = compute_adaptive_chunk_ratio(to_compact, context_window)
    max_chunk_tokens = max(int(context_window * chunk_ratio) - SUMMARIZATION_OVERHEAD_TOKENS, 2000)

    if to_compact:
        summary = await summarize_with_fallback(to_compact, model, max_chunk_tokens, timeout=timeout)
        chunks_processed = len(chunk_messages_by_max_tokens(to_compact, max_chunk_tokens))
    else:
        summary = DEFAULT_SUMMARY_FALLBACK
        chunks_processed = 0

    tokens_after = (len(summary) // 4) + (estimate_messages_tokens(final_kept) if final_kept else 0)

    return CompactionResult(
        summary=summary,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        first_kept_message_index=first_kept_index,
        chunks_processed=chunks_processed,
    )
