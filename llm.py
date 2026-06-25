"""llm.py - claw-zero's OpenAI Responses API adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


REASONING: dict[str, str] = {"effort": "xhigh", "summary": "auto"}
"""The fixed reasoning setting for every model call."""

DEFAULT_CONTEXT_TOKENS = 200_000
KNOWN_CONTEXT_WINDOWS: dict[str, int] = {"gpt-5.5": 1_050_000}

CACHE_BOUNDARY = "<!-- CLAW_ZERO_CACHE_BOUNDARY -->"
"""Marker between the byte-stable prompt prefix and volatile runtime suffix."""


@dataclass
class ToolCall:
    """One normalized tool call from the model."""

    id: str
    name: str
    arguments: str


@dataclass
class ShellCall:
    """One native local Shell call from the model."""

    id: str
    commands: list[str]
    timeout_ms: int | None = None
    max_output_length: int | None = None


@dataclass
class LLMResult:
    """Normalized result of a single Responses API call."""

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
        raise ValueError("model must be a non-empty OpenAI model id")
    model = model.strip()
    if model.lower().startswith("openai/"):
        model = model.split("/", 1)[1]
    if "/" in model:
        raise ValueError(f"claw-zero is OpenAI-only; got {model!r}")
    return model


def resolve_context_window(model: str) -> int:
    return KNOWN_CONTEXT_WINDOWS.get(_model_id(model), DEFAULT_CONTEXT_TOKENS)


def _strip_boundary(text: str) -> str:
    if CACHE_BOUNDARY not in text:
        return text
    stable, dynamic = text.split(CACHE_BOUNDARY, 1)
    return (stable.rstrip() + "\n\n" + dynamic.lstrip()).strip()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return _strip_boundary(content).strip()
    if isinstance(content, list):
        return "\n".join(
            _strip_boundary(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ).strip()
    return "" if content is None else str(content).strip()


def _function_call_item(tool_call: dict[str, Any]) -> dict[str, Any]:
    fn = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
    return {
        "type": "function_call",
        "call_id": tool_call.get("id", "") if isinstance(tool_call, dict) else "",
        "name": fn.get("name", ""),
        "arguments": fn.get("arguments", "{}"),
    }


def _remember_call_ids(
    item: dict[str, Any],
    function_call_ids: set[str],
    shell_call_ids: set[str],
) -> None:
    call_id = item.get("call_id") or item.get("id") or ""
    if not call_id:
        return
    if item.get("type") == "function_call":
        function_call_ids.add(call_id)
    elif item.get("type") == "shell_call":
        shell_call_ids.add(call_id)


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    function_call_ids: set[str] = set()
    shell_call_ids: set[str] = set()
    for msg in messages:
        item_type = msg.get("type")
        if item_type == "shell_call_output":
            call_id = msg.get("call_id", "")
            if call_id and call_id in shell_call_ids:
                items.append(dict(msg))
            continue

        role = msg.get("role", "user")

        # If this assistant turn came from Responses, replay those output items
        # directly so reasoning items stay paired with later tool outputs.
        if role == "assistant" and isinstance(msg.get("response_items"), list):
            for item in msg["response_items"]:
                if isinstance(item, dict):
                    items.append(item)
                    _remember_call_ids(item, function_call_ids, shell_call_ids)
            continue

        if role == "tool":
            call_id = msg.get("tool_call_id", "")
            if call_id and call_id in function_call_ids:
                items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _content_text(msg.get("content")),
                })
            continue

        content = _content_text(msg.get("content"))
        if content:
            items.append({"role": role, "content": content})
        if role == "assistant":
            for tc in msg.get("tool_calls", []) or []:
                item = _function_call_item(tc)
                items.append(item)
                _remember_call_ids(item, function_call_ids, shell_call_ids)
    return items


def _build_openai_kwargs(
    *,
    model: str,
    messages: list[dict[str, Any]],
    system: str | None,
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float,
    timeout: int | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": _model_id(model),
        "input": _responses_input(messages),
        "max_output_tokens": max_tokens,
        "temperature": temperature,
        "reasoning": REASONING,
    }
    if system is not None:
        kwargs["instructions"] = _strip_boundary(system)
    if tools:
        kwargs["tools"] = tools
    if timeout is not None:
        kwargs["timeout"] = timeout
    return kwargs


async def call(
    model: str,
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8192,
    temperature: float = 1.0,
    timeout: int | None = None,
) -> LLMResult:
    """Do one OpenAI Responses API call and normalize it."""
    from openai import AsyncOpenAI

    response = await AsyncOpenAI().responses.create(
        **_build_openai_kwargs(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
    )
    return _normalize(response)


def _dump_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    item_type = getattr(item, "type", "")
    if item_type == "message":
        return {
            "type": "message",
            "role": getattr(item, "role", "assistant"),
            "content": [
                {
                    "type": getattr(part, "type", ""),
                    "text": getattr(part, "text", ""),
                    "annotations": [_dump_annotation(a) for a in getattr(part, "annotations", None) or []],
                }
                for part in getattr(item, "content", None) or []
            ],
        }
    if item_type == "function_call":
        return {
            "type": "function_call",
            "call_id": getattr(item, "call_id", "") or getattr(item, "id", ""),
            "name": getattr(item, "name", ""),
            "arguments": getattr(item, "arguments", "") or "{}",
        }
    if item_type == "shell_call":
        action = getattr(item, "action", None)
        return {
            "type": "shell_call",
            "call_id": getattr(item, "call_id", "") or getattr(item, "id", ""),
            "action": _dump_shell_action(action),
            "status": getattr(item, "status", ""),
        }
    if item_type == "web_search_call":
        dumped: dict[str, Any] = {
            "type": "web_search_call",
            "id": getattr(item, "id", "") or getattr(item, "call_id", ""),
        }
        status = getattr(item, "status", "")
        action = getattr(item, "action", None)
        if status:
            dumped["status"] = status
        if action is not None:
            dumped["action"] = _dump_web_search_action(action)
        return dumped
    if item_type == "reasoning":
        dumped = {
            "type": "reasoning",
            "id": getattr(item, "id", ""),
            "summary": [_dump_summary_part(part) for part in getattr(item, "summary", None) or []],
        }
        status = getattr(item, "status", "")
        if status:
            dumped["status"] = status
        return dumped
    return {"type": item_type}


def _dump_shell_action(action: Any) -> dict[str, Any]:
    if isinstance(action, dict):
        return dict(action)
    if hasattr(action, "model_dump"):
        return action.model_dump(exclude_none=True)
    return {
        "commands": list(getattr(action, "commands", None) or []),
        "timeout_ms": getattr(action, "timeout_ms", None),
        "max_output_length": getattr(action, "max_output_length", None),
    }


def _dump_annotation(annotation: Any) -> dict[str, Any]:
    if isinstance(annotation, dict):
        return dict(annotation)
    if hasattr(annotation, "model_dump"):
        return annotation.model_dump(exclude_none=True)
    return {
        "type": getattr(annotation, "type", ""),
        "url": getattr(annotation, "url", ""),
        "title": getattr(annotation, "title", ""),
        "start_index": getattr(annotation, "start_index", None),
        "end_index": getattr(annotation, "end_index", None),
    }


def _dump_summary_part(part: Any) -> dict[str, Any]:
    if isinstance(part, dict):
        return dict(part)
    if hasattr(part, "model_dump"):
        return part.model_dump(exclude_none=True)
    return {
        "type": getattr(part, "type", ""),
        "text": getattr(part, "text", ""),
    }


def _dump_web_search_action(action: Any) -> dict[str, Any]:
    if isinstance(action, dict):
        return dict(action)
    if hasattr(action, "model_dump"):
        return action.model_dump(exclude_none=True)
    dumped: dict[str, Any] = {}
    for key in ("type", "query", "queries", "url", "pattern"):
        value = getattr(action, key, None)
        if value is not None:
            dumped[key] = value
    return dumped


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _url_citations(part: Any) -> list[tuple[str, str]]:
    citations: list[tuple[str, str]] = []
    for annotation in _get(part, "annotations", []) or []:
        if _get(annotation, "type") != "url_citation":
            continue
        url = _get(annotation, "url", "")
        if not url:
            continue
        citations.append((_get(annotation, "title", "") or url, url))
    return citations


def _output_text(item: Any) -> str:
    parts: list[str] = []
    citations: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for part in getattr(item, "content", None) or []:
        if _get(part, "type") != "output_text":
            continue
        parts.append(_get(part, "text", "") or "")
        for title, url in _url_citations(part):
            if url not in seen_urls:
                seen_urls.add(url)
                citations.append((title, url))

    text = "\n".join(p for p in parts if p).strip()
    if citations:
        sources = "\n".join(f"- {title}: {url}" for title, url in citations)
        text = f"{text}\n\nSources:\n{sources}" if text else f"Sources:\n{sources}"
    return text


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _shell_call(item: Any) -> ShellCall:
    action = _get(item, "action", {}) or {}
    commands = _get(action, "commands", []) or []
    if isinstance(commands, str):
        commands = [commands]
    return ShellCall(
        id=_get(item, "call_id", None) or _get(item, "id", "") or "",
        commands=[cmd for cmd in commands if isinstance(cmd, str) and cmd.strip()],
        timeout_ms=_optional_int(_get(action, "timeout_ms")),
        max_output_length=_optional_int(_get(action, "max_output_length")),
    )


def _normalize(response: Any) -> LLMResult:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    shell_calls: list[ShellCall] = []

    output = list(getattr(response, "output", None) or [])
    for item in output:
        item_type = getattr(item, "type", None)
        if item_type == "message":
            text = _output_text(item)
            if text:
                text_parts.append(text)
        elif item_type == "function_call":
            tool_calls.append(
                ToolCall(
                    id=getattr(item, "call_id", None) or getattr(item, "id", "") or "",
                    name=getattr(item, "name", "") or "",
                    arguments=getattr(item, "arguments", "") or "{}",
                )
            )
        elif item_type == "shell_call":
            shell_calls.append(_shell_call(item))

    usage_obj = getattr(response, "usage", None)
    usage = {
        key: int(val)
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if isinstance((val := getattr(usage_obj, key, None)), (int, float)) and val > 0
    } if usage_obj is not None else {}

    status = getattr(response, "status", "") or ""
    incomplete = getattr(response, "incomplete_details", None)
    reason = getattr(incomplete, "reason", "") if incomplete is not None else ""
    return LLMResult(
        text="\n".join(text_parts).strip(),
        tool_calls=tool_calls,
        shell_calls=shell_calls,
        finish_reason="tool_calls" if tool_calls or shell_calls else (reason or status),
        usage=usage,
        response_items=[_dump_item(item) for item in output],
    )
