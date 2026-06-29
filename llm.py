"""llm.py - claw-zero's Cerebras Chat Completions adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


REASONING_EFFORT = "high"
"""Fixed Cerebras reasoning effort for models that accept high/medium/low."""

REASONING_EFFORT_MODELS = {"gpt-oss-120b", "gemma-4-31b"}
HIDDEN_REASONING_MODELS = {"gpt-oss-120b", "zai-glm-4.7"}

DEFAULT_MAX_OUTPUT_TOKENS = 40_000
"""Default maximum completion for normal Cerebras Chat Completions calls.

Set to the Gemma 4 31B paid-tier max (40k). Models that need a different
ceiling are recorded in ``KNOWN_MAX_OUTPUT_TOKENS``."""

DEFAULT_CONTEXT_TOKENS = 128_000
KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-oss-120b": 128_000,
    "zai-glm-4.7": 128_000,
    "gemma-4-31b": 131_000,
}

KNOWN_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "gemma-4-31b": 40_000,
}
"""Per-model maximum output tokens (paid tier). Falls back to
``DEFAULT_MAX_OUTPUT_TOKENS`` when the model is not listed."""

KNOWN_TOP_P: dict[str, float] = {
    "gemma-4-31b": 0.95,
}
"""Per-model recommended ``top_p`` values from Cerebras docs.
None → omit ``top_p`` from the API call (provider default)."""

DEFAULT_AUTO_COMPACT_RATIO = 0.6
"""Codex-style fallback: compact around half the configured context window."""

DEFAULT_TOOL_OUTPUT_TOKENS = 12_000
"""Approximate per-tool-output budget, matching Codex's public config example."""

CACHE_BOUNDARY = "<!-- CLAW_ZERO_CACHE_BOUNDARY -->"
"""Marker between the byte-stable prompt prefix and volatile runtime suffix."""

SHELL_TOOL_NAME = "shell"
"""Synthetic Cerebras function-tool name for claw-zero's local Shell tool."""

_UNSUPPORTED_HOSTED_TOOL_TYPES = {"web_search"}
_ASYNC_CLIENT: Any | None = None


@dataclass
class ToolCall:
    """One normalized tool call from the model."""

    id: str
    name: str
    arguments: str


@dataclass
class ShellCall:
    """One local Shell call requested through the synthetic Cerebras tool."""

    id: str
    commands: list[str]
    timeout_ms: int | None = None
    max_output_length: int | None = None


@dataclass
class LLMResult:
    """Normalized result of a single Cerebras Chat Completions call."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    shell_calls: list[ShellCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    response_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls or self.shell_calls)


def _model_id(model: str) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty Cerebras model id")
    model = model.strip()
    if model.lower().startswith("cerebras/"):
        model = model.split("/", 1)[1]
    if "/" in model:
        raise ValueError(f"claw-zero is currently Cerebras-only; got {model!r}")
    return model


def resolve_context_window(model: str) -> int:
    return KNOWN_CONTEXT_WINDOWS.get(_model_id(model), DEFAULT_CONTEXT_TOKENS)


def resolve_max_output_tokens(model: str) -> int:
    """Return the paid-tier max output token ceiling for *model*."""
    return KNOWN_MAX_OUTPUT_TOKENS.get(_model_id(model), DEFAULT_MAX_OUTPUT_TOKENS)


def default_auto_compact_token_limit(context_window: int) -> int:
    """Default compact trigger when no explicit token limit is configured."""
    if context_window <= 0:
        raise ValueError(f"context_window must be positive, got {context_window!r}")
    return max(1, int(context_window * DEFAULT_AUTO_COMPACT_RATIO))


def _strip_boundary(text: str) -> str:
    if CACHE_BOUNDARY not in text:
        return text
    stable, dynamic = text.split(CACHE_BOUNDARY, 1)
    return (stable.rstrip() + "\n\n" + dynamic.lstrip()).strip()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return _strip_boundary(content).strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(_strip_boundary(text))
        return "\n".join(parts).strip()
    return "" if content is None else str(content).strip()


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _tool_result_ids(messages: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for msg in messages:
        if msg.get("type") == "shell_call_output":
            call_id = msg.get("call_id")
            if isinstance(call_id, str) and call_id:
                ids.add(call_id)
        elif msg.get("role") == "tool":
            call_id = msg.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                ids.add(call_id)
    return ids


def _dump_tool_call(tool_call: Any) -> dict[str, Any]:
    fn = _get(tool_call, "function", {}) or {}
    return {
        "id": _get(tool_call, "id", "") or _get(tool_call, "call_id", "") or "",
        "type": _get(tool_call, "type", "function") or "function",
        "function": {
            "name": _get(fn, "name", "") or "",
            "arguments": _get(fn, "arguments", "{}") or "{}",
        },
    }


def _function_tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments or "{}"},
    }


def _shell_arguments_from_action(action: dict[str, Any]) -> str:
    args: dict[str, Any] = {"commands": action.get("commands", []) or []}
    if action.get("timeout_ms") is not None:
        args["timeout_ms"] = action["timeout_ms"]
    if action.get("max_output_length") is not None:
        args["max_output_length"] = action["max_output_length"]
    return json.dumps(args, ensure_ascii=False, default=str)


def _message_text_from_response_item(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for part in item.get("content", []) or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") in {"output_text", "text"} and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts).strip()


def _assistant_from_response_items(
    response_items: list[dict[str, Any]],
    result_ids: set[str],
) -> dict[str, Any] | None:
    """Convert stored assistant output metadata back to chat-completions shape.

    New Cerebras calls persist one chat-shaped assistant item. This helper also
    understands legacy response-item shapes well enough to avoid losing
    function/shell pairing in existing transcripts.
    """
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for item in response_items:
        if not isinstance(item, dict):
            continue

        if item.get("role") == "assistant":
            content = _content_text(item.get("content"))
            raw_calls = item.get("tool_calls", []) or []
            calls = [
                _dump_tool_call(tc)
                for tc in raw_calls
                if isinstance(tc, dict) and (_dump_tool_call(tc).get("id") in result_ids)
            ]
            if not content and not calls:
                return None
            msg: dict[str, Any] = {"role": "assistant", "content": content}
            if calls:
                msg["tool_calls"] = calls
            return msg

        item_type = item.get("type")
        if item_type == "message":
            text = _message_text_from_response_item(item)
            if text:
                content_parts.append(text)
        elif item_type == "function_call":
            call_id = item.get("call_id") or item.get("id") or ""
            if call_id in result_ids:
                tool_calls.append(_function_tool_call(
                    call_id,
                    item.get("name", "") or "",
                    item.get("arguments", "{}") or "{}",
                ))
        elif item_type == "shell_call":
            call_id = item.get("call_id") or item.get("id") or ""
            action = item.get("action", {}) if isinstance(item.get("action"), dict) else {}
            if call_id in result_ids:
                tool_calls.append(_function_tool_call(
                    call_id,
                    SHELL_TOOL_NAME,
                    _shell_arguments_from_action(action),
                ))

    content = "\n".join(content_parts).strip()
    if not content and not tool_calls:
        return None
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _shell_output_content(msg: dict[str, Any]) -> str:
    output = msg.get("output")
    if output is None:
        output = {k: v for k, v in msg.items() if k not in {"type", "call_id"}}
    return _json_text(output)


def _append_assistant_message(
    out: list[dict[str, Any]],
    *,
    content: str,
    tool_calls: list[dict[str, Any]],
    seen_call_ids: set[str],
) -> None:
    if not content and not tool_calls:
        return
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
        seen_call_ids.update(
            tc["id"] for tc in tool_calls if isinstance(tc.get("id"), str) and tc["id"]
        )
    out.append(msg)


def _chat_messages(messages: list[dict[str, Any]], *, system: str | None = None) -> list[dict[str, Any]]:
    """Convert claw-zero's internal chat-ish history to Cerebras chat messages."""
    out: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()
    result_ids = _tool_result_ids(messages)

    if system is not None:
        system_text = _strip_boundary(system).strip()
        if system_text:
            out.append({"role": "system", "content": system_text})

    for msg in messages:
        item_type = msg.get("type")
        if item_type == "shell_call_output":
            call_id = msg.get("call_id", "")
            if call_id and call_id in seen_call_ids:
                out.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _shell_output_content(msg),
                })
            continue

        role = msg.get("role", "user")
        if role == "tool":
            call_id = msg.get("tool_call_id", "")
            if call_id and call_id in seen_call_ids:
                out.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _content_text(msg.get("content")),
                })
            continue

        if role == "assistant":
            response_items = msg.get("response_items")
            if isinstance(response_items, list):
                replay = _assistant_from_response_items(response_items, result_ids)
                if replay is not None:
                    calls = replay.get("tool_calls", []) or []
                    _append_assistant_message(
                        out,
                        content=_content_text(replay.get("content")),
                        tool_calls=[tc for tc in calls if isinstance(tc, dict)],
                        seen_call_ids=seen_call_ids,
                    )
                    continue

            raw_calls = msg.get("tool_calls", []) or []
            calls = [
                _dump_tool_call(tc)
                for tc in raw_calls
                if isinstance(tc, dict) and (_dump_tool_call(tc).get("id") in result_ids)
            ]
            _append_assistant_message(
                out,
                content=_content_text(msg.get("content")),
                tool_calls=calls,
                seen_call_ids=seen_call_ids,
            )
            continue

        if role not in {"system", "user", "developer"}:
            role = "user"
        content = _content_text(msg.get("content"))
        if content:
            out.append({"role": role, "content": content})
    return out


def _shell_tool_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": SHELL_TOOL_NAME,
            "description": (
                "Run shell commands on the local machine through claw-zero's "
                "configured local Shell executor."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One or more shell commands to execute in order.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Optional timeout in milliseconds.",
                    },
                    "max_output_length": {
                        "type": "integer",
                        "description": "Optional maximum captured output length in characters.",
                    },
                },
                "required": ["commands"],
            },
        },
    }


def _cerebras_tool(tool: dict[str, Any]) -> dict[str, Any] | None:
    tool_type = tool.get("type")
    if tool_type in _UNSUPPORTED_HOSTED_TOOL_TYPES:
        # Cerebras Chat Completions supports client-executed function tools, not
        # hosted provider tools. Do not advertise unsupported hosted specs to
        # the model.
        return None
    if tool_type == "shell":
        return _shell_tool_spec()
    if tool_type != "function":
        return None

    if isinstance(tool.get("function"), dict):
        fn = dict(tool["function"])
    else:
        fn = {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        }
    if not fn.get("name"):
        return None
    return {"type": "function", "function": fn}


def _cerebras_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not tools:
        return []
    converted: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        converted_tool = _cerebras_tool(tool)
        if converted_tool is None:
            continue
        name = converted_tool.get("function", {}).get("name")
        if not isinstance(name, str) or not name or name in seen_names:
            continue
        seen_names.add(name)
        converted.append(converted_tool)
    return converted


def _reasoning_kwargs(model: str) -> dict[str, Any]:
    model_id = _model_id(model)
    kwargs: dict[str, Any] = {}
    if model_id in REASONING_EFFORT_MODELS:
        kwargs["reasoning_effort"] = REASONING_EFFORT
    if model_id in HIDDEN_REASONING_MODELS:
        kwargs["reasoning_format"] = "hidden"
    return kwargs


def _build_cerebras_kwargs(
    *,
    model: str,
    messages: list[dict[str, Any]],
    system: str | None,
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float,
    top_p: float | None,
    timeout: int | None,
) -> dict[str, Any]:
    model_id = _model_id(model)
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": _chat_messages(messages, system=system),
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
        **_reasoning_kwargs(model),
    }
    # Apply per-model top_p recommendation when the caller doesn't override.
    effective_top_p = top_p if top_p is not None else KNOWN_TOP_P.get(model_id)
    if effective_top_p is not None:
        kwargs["top_p"] = effective_top_p
    converted_tools = _cerebras_tools(tools)
    if converted_tools:
        kwargs["tools"] = converted_tools
        kwargs["parallel_tool_calls"] = True
    if timeout is not None:
        kwargs["timeout"] = timeout
    return kwargs


def _client() -> Any:
    global _ASYNC_CLIENT
    if _ASYNC_CLIENT is None:
        from cerebras.cloud.sdk import AsyncCerebras

        _ASYNC_CLIENT = AsyncCerebras(warm_tcp_connection=False)
    return _ASYNC_CLIENT


async def call(
    model: str,
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    temperature: float = 1.0,
    top_p: float | None = None,
    timeout: int | None = None,
) -> LLMResult:
    """Do one Cerebras Chat Completions API call and normalize it."""
    response = await _client().chat.completions.create(
        **_build_cerebras_kwargs(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            timeout=timeout,
        )
    )
    return _normalize(response)


async def count_input_tokens(
    model: str,
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    timeout: int | None = None,
) -> int:
    """Return a conservative local input-token estimate for Cerebras.

    The Cerebras SDK exposes usage after a completion, but not a preflight
    token-count endpoint. Keep the public function so callers can share the same
    budget path, using a serialized chat request estimate instead of making a
    network call.
    """
    del timeout  # no preflight API request is made
    payload = {
        "model": _model_id(model),
        "messages": _chat_messages(messages, system=system),
    }
    converted_tools = _cerebras_tools(tools)
    if converted_tools:
        payload["tools"] = converted_tools
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
    return max(1, int(len(raw) / 4 * 1.2))


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _shell_call_from_tool_call(tool_call: Any) -> ShellCall:
    dumped = _dump_tool_call(tool_call)
    fn = dumped["function"]
    args = _parse_arguments(fn.get("arguments"))
    commands = args.get("commands")
    if commands is None:
        commands = args.get("command") or args.get("cmd") or []
    if isinstance(commands, str):
        commands = [commands]
    return ShellCall(
        id=dumped.get("id", "") or "",
        commands=[cmd for cmd in commands if isinstance(cmd, str) and cmd.strip()],
        timeout_ms=_optional_int(args.get("timeout_ms")),
        max_output_length=_optional_int(args.get("max_output_length")),
    )


def _usage_dict(usage_obj: Any) -> dict[str, int]:
    if usage_obj is None:
        return {}
    usage: dict[str, int] = {}
    mapping = {
        "prompt_tokens": "input_tokens",
        "completion_tokens": "output_tokens",
        "total_tokens": "total_tokens",
    }
    for source_key, target_key in mapping.items():
        value = _get(usage_obj, source_key)
        if isinstance(value, (int, float)) and value >= 0:
            usage[target_key] = int(value)

    prompt_details = _get(usage_obj, "prompt_tokens_details")
    cached_tokens = _get(prompt_details, "cached_tokens") if prompt_details is not None else None
    if isinstance(cached_tokens, (int, float)) and cached_tokens >= 0:
        usage["cached_input_tokens"] = int(cached_tokens)

    completion_details = _get(usage_obj, "completion_tokens_details")
    reasoning_tokens = _get(completion_details, "reasoning_tokens") if completion_details is not None else None
    if isinstance(reasoning_tokens, (int, float)) and reasoning_tokens >= 0:
        usage["reasoning_output_tokens"] = int(reasoning_tokens)
    return usage


def _dump_assistant_response_item(message: Any) -> dict[str, Any] | None:
    content = _get(message, "content", "") or ""
    tool_calls = [_dump_tool_call(tc) for tc in (_get(message, "tool_calls", []) or [])]
    if not content and not tool_calls:
        return None
    item: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        item["tool_calls"] = tool_calls
    return item


def _normalize(response: Any) -> LLMResult:
    choices = list(_get(response, "choices", []) or [])
    choice = choices[0] if choices else None
    message = _get(choice, "message") if choice is not None else None

    text = _get(message, "content", "") if message is not None else ""
    if text is None:
        text = ""
    text = _content_text(text)

    tool_calls: list[ToolCall] = []
    shell_calls: list[ShellCall] = []
    for raw_call in (_get(message, "tool_calls", []) or []) if message is not None else []:
        dumped = _dump_tool_call(raw_call)
        fn = dumped["function"]
        name = fn.get("name", "") or ""
        if name == SHELL_TOOL_NAME:
            shell_call = _shell_call_from_tool_call(raw_call)
            if shell_call.commands:
                shell_calls.append(shell_call)
            else:
                tool_calls.append(ToolCall(
                    id=dumped.get("id", "") or "",
                    name=name,
                    arguments=fn.get("arguments", "{}") or "{}",
                ))
            continue
        tool_calls.append(ToolCall(
            id=dumped.get("id", "") or "",
            name=name,
            arguments=fn.get("arguments", "{}") or "{}",
        ))

    finish_reason = _get(choice, "finish_reason", "") if choice is not None else ""
    assistant_item = _dump_assistant_response_item(message) if message is not None else None
    return LLMResult(
        text=text,
        tool_calls=tool_calls,
        shell_calls=shell_calls,
        finish_reason="tool_calls" if tool_calls or shell_calls else (finish_reason or ""),
        usage=_usage_dict(_get(response, "usage")),
        response_items=[assistant_item] if assistant_item is not None else [],
    )
