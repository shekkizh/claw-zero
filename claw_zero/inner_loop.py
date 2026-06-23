"""Inner loop — one activation → one delivered message.

An activation runs the model in a loop: it may make tool calls (bash is the only
client-side tool), and it **ends by delivering a message** — the model's
plain-text reply, with no tool call. That delivered ``Message`` is the return
value; it replaces the harness's ``DONE`` signal entirely. There is no terminal
state — the outer loop calls this again for the next incoming message.

Flow (matching the Phase 7 brief):

    loop:
        flush_memory_if_triggered()                 # Phase 5 — before compaction
        result = llm.call(system, messages, tools)  # Phase 2 — effort fixed at max
        append assistant turn to messages + transcript
        if result.tool_calls:                       # bash is the only tool
            for call in result.tool_calls:
                out = handlers[call.name](args)
                append tool result (role="tool", tool_call_id=...)
            if over_budget(): compact_in_place()     # Phase 6
            continue
        else:                                        # plain text → deliver
            return Message(sender=self_id, recipient=incoming.sender, content=result.text)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import llm
from .context.compaction import compact_messages
from .context.token_estimation import SAFETY_MARGIN, estimate_messages_tokens
from .context.transcript import Transcript
from .memory.flush import FlushState, maybe_flush_memory
from .memory.store import MemoryStore
from .messaging.mailbox import Message
from .tools.registry import ToolRegistry


# Safety cap on tool iterations within a single activation. Not a user knob — a
# backstop against a model that calls tools forever without ever replying.
DEFAULT_MAX_TOOL_ITERATIONS = 50


@dataclass
class ActivationContext:
    """Everything one activation needs. The durable fields (``messages``,
    ``transcript``, ``memory_store``, ``flush_state``) are shared across
    activations by the owning agent — claw-zero is long-running, so the
    conversation persists.

    Attributes:
        model: litellm model string.
        system_prompt: The assembled system prompt for this activation.
        messages: The running chat-shape conversation (mutated in place).
        tools: The single-tool registry (specs + handlers).
        memory_store: Durable memory backend (read/written by the flush turn;
            the agent itself reaches memory via bash).
        transcript: Append-only JSONL log.
        flush_state: Pre-compaction flush bookkeeping.
        incoming: The message that triggered this activation.
        context_window: Model context window in tokens.
        compaction_threshold: Fraction of the window that triggers compaction.
        max_tool_result_chars: Per-tool-result content cap.
        instructions_tokens: Estimated system-prompt token count (budget input).
    """

    model: str
    system_prompt: str
    messages: list[dict[str, Any]]
    tools: ToolRegistry
    memory_store: MemoryStore
    transcript: Transcript
    flush_state: FlushState
    incoming: Message
    context_window: int = 200_000
    compaction_threshold: float = 0.8
    max_tool_result_chars: int = 16_000
    instructions_tokens: int = field(default=0)

    @property
    def agent_id(self) -> str:
        return self.incoming.recipient


def _assistant_message(result: "llm.LLMResult") -> dict[str, Any]:
    """Reconstruct an OpenAI assistant message from an ``LLMResult``.

    Includes the ``tool_calls`` array (so the following tool messages pair by id)
    when present. ``content`` may be empty text alongside tool calls.
    """
    msg: dict[str, Any] = {"role": "assistant", "content": result.text or ""}
    if result.tool_calls:
        msg["tool_calls"] = [
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
            for tc in result.tool_calls
        ]
    return msg


def _format_tool_result(result: dict[str, Any], cap: int) -> str:
    """Render a tool handler's result dict as the tool message content (capped)."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > cap:
        text = text[: cap - 40] + "\n[... tool result truncated ...]"
    return text


async def _dispatch_tool(ctx: ActivationContext, tool_call: "llm.ToolCall") -> dict[str, Any]:
    """Run one tool call, returning the ``{"role": "tool", ...}`` message."""
    handler = ctx.tools.handlers.get(tool_call.name)
    if handler is None:
        content = json.dumps({"success": False, "error": f"Unknown tool: {tool_call.name!r}"})
        return {"role": "tool", "tool_call_id": tool_call.id, "content": content}

    try:
        args = json.loads(tool_call.arguments) if tool_call.arguments else {}
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        content = json.dumps({"success": False, "error": f"Invalid tool arguments: {exc}"})
        return {"role": "tool", "tool_call_id": tool_call.id, "content": content}

    try:
        result = await handler(args)
    except Exception as exc:  # noqa: BLE001 — a tool crash must not kill the loop
        result = {"success": False, "error": f"Tool {tool_call.name!r} raised: {exc}"}

    return {
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": _format_tool_result(result, ctx.max_tool_result_chars),
    }


def _current_tokens(ctx: ActivationContext) -> int:
    """Estimated prompt tokens = instructions + scaled message estimate."""
    raw = estimate_messages_tokens(ctx.messages)
    return int(raw * SAFETY_MARGIN) + ctx.instructions_tokens


def _over_budget(ctx: ActivationContext) -> bool:
    return _current_tokens(ctx) > ctx.context_window * ctx.compaction_threshold


async def _compact_in_place(ctx: ActivationContext) -> None:
    """Summarize older history into a checkpoint message; rebuild ``messages``.

    Increments the flush-state compaction counter and records a compaction entry
    in the transcript. The summary is injected as a leading user message so the
    model still sees the prior context as a checkpoint.
    """
    result = await compact_messages(
        ctx.messages,
        ctx.model,
        ctx.context_window,
        instructions_tokens=ctx.instructions_tokens,
        recent_turns_preserve=3,
    )
    kept = ctx.messages[result.first_kept_message_index:]
    summary_msg = {
        "role": "user",
        "content": f"[Earlier context summary — prior turns were compacted]\n\n{result.summary}",
    }
    ctx.messages[:] = [summary_msg, *kept]
    ctx.flush_state.compaction_count += 1
    entry_id = ctx.transcript.append_compaction(
        result.summary, first_kept_entry_id="", tokens_before=result.tokens_before
    )
    print(
        f"[Compaction] ~{result.tokens_before} → ~{result.tokens_after} tokens "
        f"({result.chunks_processed} chunks); entry {entry_id}"
    )


async def run(ctx: ActivationContext) -> Message:
    """Run one activation and return the message to deliver to the peer.

    The incoming message has already been appended to ``ctx.messages`` by the
    caller (the outer loop), so this drives the model from the current
    conversation state.
    """
    for _ in range(DEFAULT_MAX_TOOL_ITERATIONS):
        # 1. Pre-API memory flush (before any compaction can summarize context away).
        await maybe_flush_memory(
            model=ctx.model,
            messages=ctx.messages,
            memory_store=ctx.memory_store,
            state=ctx.flush_state,
            current_tokens=_current_tokens(ctx),
            context_window=ctx.context_window,
            transcript_bytes=ctx.transcript.transcript_bytes,
            compaction_ratio=ctx.compaction_threshold,
        )

        # 2. The model call (effort fixed at max inside llm.call).
        result = await llm.call(
            ctx.model,
            ctx.messages,
            system=ctx.system_prompt,
            tools=ctx.tools.specs,
        )

        # 3. Append the assistant turn to messages + transcript.
        assistant_msg = _assistant_message(result)
        ctx.messages.append(assistant_msg)
        ctx.transcript.append_message(
            "assistant",
            assistant_msg.get("content") or "",
            usage=result.usage or None,
            stop_reason=result.finish_reason or None,
        )

        if result.has_tool_calls:
            # 4. Execute each tool call; append its result message.
            for tool_call in result.tool_calls:
                tool_msg = await _dispatch_tool(ctx, tool_call)
                ctx.messages.append(tool_msg)
                ctx.transcript.append_message("tool", tool_msg["content"])
            # 5. Compact in place if we've crossed the budget.
            if _over_budget(ctx):
                await _compact_in_place(ctx)
            continue

        # 6. No tool call → the plain-text reply IS the delivered message.
        return Message(
            sender=ctx.agent_id,
            recipient=ctx.incoming.sender,
            content=result.text,
        )

    # Safety backstop: the model kept calling tools without ever replying.
    return Message(
        sender=ctx.agent_id,
        recipient=ctx.incoming.sender,
        content=(
            "I hit the per-activation tool-call limit without reaching a reply. "
            "Stopping here; send another message to continue."
        ),
    )
