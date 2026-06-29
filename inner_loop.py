"""Inner loop — one activation → one delivered message.

An activation runs the model in a loop: it may make tool calls (local Shell is
the client-side command tool), and it **ends by delivering a message** — the model's
plain-text reply, with no tool call. That delivered ``Message`` is the return
value; it replaces the harness's ``DONE`` signal entirely. There is no terminal
state — the outer loop calls this again for the next incoming message.

Flow (matching the Phase 7 brief):

    loop:
        flush_memory_if_triggered()                 # Phase 5 — before compaction
        if over_budget(): compact_in_place()        # Phase 6
        result = llm.call(system, messages, tools)  # Phase 2 - Cerebras Chat Completions
        append assistant turn to messages + transcript
        if result.has_tool_calls:
            execute local shell/function calls
            append tool outputs
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
from .context.token_estimation import SAFETY_MARGIN, estimate_messages_tokens, token_limit_to_char_cap
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
        model: Cerebras model id.
        system_prompt: The assembled system prompt for this activation.
        messages: The running chat-shape conversation (mutated in place).
        tools: The tool registry (hosted specs plus local handlers).
        memory_store: Durable memory backend (read/written by the flush turn;
            the agent itself reaches memory via shell).
        transcript: Append-only JSONL log.
        flush_state: Pre-compaction flush bookkeeping.
        incoming: The message that triggered this activation.
        context_window: Model context window in tokens.
        auto_compact_token_limit: Prompt-token count that triggers compaction.
        tool_output_token_limit: Approximate per-tool-result content cap.
        instructions_tokens: Estimated system-prompt token count (budget input).
        last_api_input_tokens: Most recent API-reported prompt size, used as a
            floor on heuristic estimates until the next compaction.
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
    auto_compact_token_limit: int = 100_000
    tool_output_token_limit: int = llm.DEFAULT_TOOL_OUTPUT_TOKENS
    instructions_tokens: int = field(default=0)
    last_api_input_tokens: int = field(default=0)

    @property
    def agent_id(self) -> str:
        return self.incoming.recipient


def _assistant_message(result: "llm.LLMResult") -> dict[str, Any]:
    """Reconstruct an assistant message from an ``LLMResult``.

    Keeps the normalized ``tool_calls`` array for local pairing/serialization and
    the provider metadata so the next API request can replay tool calls.
    """
    msg: dict[str, Any] = {"role": "assistant", "content": result.text or ""}
    if result.tool_calls:
        msg["tool_calls"] = [
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
            for tc in result.tool_calls
        ]
    if result.response_items:
        msg["response_items"] = result.response_items
    return msg


def _transcript_tool_calls(result: "llm.LLMResult") -> list[dict[str, Any]]:
    """Build a transcript-friendly list of function, Shell, and hosted calls."""
    calls: list[dict[str, Any]] = [
        {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
        for tc in result.tool_calls
    ]
    for shell_call in result.shell_calls:
        shell: dict[str, Any] = {"commands": shell_call.commands}
        if shell_call.timeout_ms is not None:
            shell["timeout_ms"] = shell_call.timeout_ms
        if shell_call.max_output_length is not None:
            shell["max_output_length"] = shell_call.max_output_length
        calls.append({"id": shell_call.id, "type": "shell", "shell": shell})
    calls.extend(_transcript_web_search_calls(result.response_items))
    return calls


def _transcript_web_search_calls(response_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in response_items:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        call: dict[str, Any] = {"id": item.get("id") or item.get("call_id") or "", "type": "web_search"}
        if item.get("status"):
            call["status"] = item["status"]
        if isinstance(item.get("action"), dict):
            call["action"] = dict(item["action"])
        calls.append(call)
    return calls


def _transcript_reasoning_summaries(response_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in response_items:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        summary = item.get("summary")
        if not isinstance(summary, list) or not summary:
            continue
        reasoning_summary: dict[str, Any] = {
            "id": item.get("id", ""),
            "summary": [part for part in summary if isinstance(part, dict)],
        }
        if item.get("status"):
            reasoning_summary["status"] = item["status"]
        summaries.append(reasoning_summary)
    return summaries


def _transcript_tool_results(result: "llm.LLMResult") -> list[dict[str, Any]]:
    """Build transcript metadata for hosted tool results returned in-place."""
    web_call_ids = [
        call["id"]
        for call in _transcript_web_search_calls(result.response_items)
        if isinstance(call.get("id"), str) and call["id"]
    ]
    if not web_call_ids:
        return []

    results: list[dict[str, Any]] = []
    for item in result.response_items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        result_item: dict[str, Any] = {
            "type": "web_search_result",
            "toolCallIds": web_call_ids,
            "content": [part for part in content if isinstance(part, dict)],
        }
        if len(web_call_ids) == 1:
            result_item["toolCallId"] = web_call_ids[0]
        if item.get("id"):
            result_item["id"] = item["id"]
        if item.get("status"):
            result_item["status"] = item["status"]
        results.append(result_item)
    return results


def _format_tool_result(result: dict[str, Any], token_limit: int) -> str:
    """Render a tool handler's result dict as the tool message content (capped)."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    cap = token_limit_to_char_cap(token_limit)
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
        "content": _format_tool_result(result, ctx.tool_output_token_limit),
    }


def _shell_call_output(
    shell_call: "llm.ShellCall",
    *,
    stderr: str,
    outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "shell_call_output",
        "call_id": shell_call.id,
        "output": [
            {
                "stdout": "",
                "stderr": stderr,
                "outcome": outcome or {"type": "exit", "exit_code": 1},
            }
        ],
    }
    if shell_call.max_output_length is not None:
        payload["max_output_length"] = shell_call.max_output_length
    return payload


async def _dispatch_shell(ctx: ActivationContext, shell_call: "llm.ShellCall") -> dict[str, Any]:
    """Run one native local Shell call, returning ``shell_call_output``."""
    handler = ctx.tools.shell_handler
    if handler is None:
        return _shell_call_output(shell_call, stderr="Error: local shell executor is not configured.")

    try:
        result = await handler({
            "call_id": shell_call.id,
            "commands": shell_call.commands,
            "timeout_ms": shell_call.timeout_ms,
            "max_output_length": shell_call.max_output_length,
        })
    except Exception as exc:  # noqa: BLE001 — a shell crash must not kill the loop
        return _shell_call_output(shell_call, stderr=f"Shell executor raised: {exc}")

    if not isinstance(result, dict) or result.get("type") != "shell_call_output":
        return _shell_call_output(shell_call, stderr="Error: shell executor returned an invalid result.")
    return result


def _estimated_tokens(ctx: ActivationContext) -> int:
    """Current heuristic prompt tokens including instructions."""
    raw = estimate_messages_tokens(ctx.messages)
    return int(raw * SAFETY_MARGIN) + ctx.instructions_tokens


def _current_tokens(ctx: ActivationContext) -> int:
    """Current prompt tokens, with API-reported usage as the floor when known."""
    return max(_estimated_tokens(ctx), ctx.last_api_input_tokens)


async def _current_tokens_for_budget(ctx: ActivationContext) -> int:
    """Use the LLM adapter preflight token counter before compacting on a heuristic estimate."""
    estimated = _estimated_tokens(ctx)
    current = max(estimated, ctx.last_api_input_tokens)
    if estimated <= ctx.auto_compact_token_limit or ctx.last_api_input_tokens >= estimated:
        return current
    try:
        exact = await llm.count_input_tokens(
            ctx.model,
            ctx.messages,
            system=ctx.system_prompt,
            tools=ctx.tools.specs,
        )
    except Exception as exc:  # non-fatal: estimation remains the fallback
        print(f"[TokenCount] failed (using estimate): {exc}")
        return current
    if exact > 0:
        ctx.last_api_input_tokens = exact
        return exact
    return current


def _over_budget(ctx: ActivationContext, current_tokens: int | None = None) -> bool:
    tokens = current_tokens if current_tokens is not None else _current_tokens(ctx)
    return tokens > ctx.auto_compact_token_limit


def _record_usage(ctx: ActivationContext, result: "llm.LLMResult") -> None:
    input_tokens = result.usage.get("input_tokens") if result.usage else None
    if isinstance(input_tokens, int) and input_tokens > 0:
        ctx.last_api_input_tokens = input_tokens


async def _maybe_flush_then_compact(ctx: ActivationContext) -> None:
    current_tokens = await _current_tokens_for_budget(ctx)
    await maybe_flush_memory(
        model=ctx.model,
        messages=ctx.messages,
        memory_store=ctx.memory_store,
        state=ctx.flush_state,
        current_tokens=current_tokens,
        context_window=ctx.context_window,
        transcript_bytes=ctx.transcript.transcript_bytes,
        auto_compact_token_limit=ctx.auto_compact_token_limit,
    )
    if _over_budget(ctx, current_tokens):
        await _compact_in_place(ctx)


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
    ctx.last_api_input_tokens = 0
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
        # 1. Pre-API memory flush, then compact if the current prompt is already
        # over budget. The flush runs first so context is durable before shrink.
        await _maybe_flush_then_compact(ctx)

        # 2. The model call (reasoning fixed inside llm.call).
        result = await llm.call(
            ctx.model,
            ctx.messages,
            system=ctx.system_prompt,
            tools=ctx.tools.specs,
        )
        _record_usage(ctx, result)

        # 3. Append the assistant turn to messages + transcript.
        assistant_msg = _assistant_message(result)
        reasoning_summaries = _transcript_reasoning_summaries(result.response_items)
        transcript_tool_calls = _transcript_tool_calls(result)
        transcript_tool_results = _transcript_tool_results(result)
        ctx.messages.append(assistant_msg)
        ctx.transcript.append_message(
            "assistant",
            assistant_msg.get("content") or "",
            usage=result.usage or None,
            stop_reason=result.finish_reason or None,
            reasoning_summaries=reasoning_summaries,
            tool_calls=transcript_tool_calls,
            tool_results=transcript_tool_results,
        )

        if result.has_tool_calls:
            # 4. Execute each local Shell/function call; append its result item.
            for shell_call in result.shell_calls:
                shell_msg = await _dispatch_shell(ctx, shell_call)
                ctx.messages.append(shell_msg)
                ctx.transcript.append_message(
                    "shell",
                    json.dumps(shell_msg, ensure_ascii=False, default=str),
                    tool_call_id=shell_call.id,
                )
            for tool_call in result.tool_calls:
                tool_msg = await _dispatch_tool(ctx, tool_call)
                ctx.messages.append(tool_msg)
                ctx.transcript.append_message("tool", tool_msg["content"], tool_call_id=tool_call.id)
            # 5. Compact in place if we've crossed the budget.
            await _maybe_flush_then_compact(ctx)
            continue

        # 6. No tool call → the plain-text reply IS the delivered message. We
        # still compact first if needed so the next activation starts under budget.
        await _maybe_flush_then_compact(ctx)
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
