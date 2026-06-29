"""Tool registry - assemble the tool surface the loop needs.

Mirrors the harness ``tools/tools.py`` ``build_tools`` / ``get_tool_summaries``
split so the prompt builder reads tool summaries the same way, except the
surface is minimal. Returns Cerebras-compatible tool specs (for the LLM call), a
``name -> handler`` map for local function tools, a local Shell executor, and
the ``name -> one-line summary`` map for the prompt builder.

Cerebras Chat Completions supports client-executed function tools. The local
Shell is advertised to the model as a synthetic function tool by ``llm.py`` and
executed here through the ``BashTool`` shell handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol


class Tool(Protocol):
    """The minimal tool contract the registry needs."""

    name: str

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict[str, Any]: ...

    async def run(self, params: dict[str, Any]) -> dict[str, Any]: ...


# A handler takes parsed args and returns a JSON-serializable result dict.
ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ShellHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

LOCAL_SHELL_SPEC: dict[str, Any] = {
    "type": "shell",
    "environment": {"type": "local"},
}
LOCAL_SHELL_SUMMARY = "Run shell commands in the local workspace runtime."


@dataclass
class ToolRegistry:
    """The assembled tool surface: specs (for the API), handlers, and summaries."""

    specs: list[dict[str, Any]]
    handlers: dict[str, ToolHandler]
    shell_handler: ShellHandler | None
    shell_tool: Any | None
    summaries: dict[str, str]


def _to_cerebras_source_spec(tool: Tool) -> dict[str, Any]:
    """Render a local tool in claw-zero's flat function-tool source shape."""
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def build_tools(*tools: Tool) -> ToolRegistry:
    """Assemble a ``ToolRegistry`` from the given tools.

    Local Shell is included by default and backed by the ``BashTool`` instance
    if one is supplied. Other local tools get
    function handlers so the loop can dispatch function calls emitted by the
    model.
    """
    specs: list[dict[str, Any]] = [dict(LOCAL_SHELL_SPEC)]
    handlers: dict[str, ToolHandler] = {}
    shell_handler: ShellHandler | None = None
    shell_tool: Any | None = None
    summaries: dict[str, str] = {
        "shell": LOCAL_SHELL_SUMMARY,
    }
    for tool in tools:
        run_shell_call = getattr(tool, "run_shell_call", None)
        if callable(run_shell_call):
            shell_handler = run_shell_call
            shell_tool = tool
            summaries["shell"] = tool.description.split("\n", 1)[0].strip()
            continue
        specs.append(_to_cerebras_source_spec(tool))
        handlers[tool.name] = tool.run
        # The prompt's Tools section shows a one-line summary: first line of the
        # full description (the rest is the model-facing detail, already in the
        # tool spec sent to the API).
        summaries[tool.name] = tool.description.split("\n", 1)[0].strip()
    return ToolRegistry(
        specs=specs,
        handlers=handlers,
        shell_handler=shell_handler,
        shell_tool=shell_tool,
        summaries=summaries,
    )


def get_tool_summaries(registry: ToolRegistry) -> dict[str, str]:
    """Return the ``name -> one-line summary`` map for the prompt builder."""
    return registry.summaries
