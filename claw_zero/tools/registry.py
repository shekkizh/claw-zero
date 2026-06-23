"""Tool registry — assemble the single-tool surface the loop needs.

Mirrors the harness ``tools/tools.py`` ``build_tools`` / ``get_tool_summaries``
split so the prompt builder reads tool summaries the same way, except the
surface is exactly one tool: ``bash``. Returns OpenAI-format tool specs (for the
LLM call) plus a ``name -> handler`` map (for the loop to dispatch on), and the
``name -> one-line summary`` map (for the prompt builder).

Adding more tools later is a matter of appending to ``ToolRegistry`` — the rest
of the loop is tool-agnostic. (More tools are deferred by design.)
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


@dataclass
class ToolRegistry:
    """The assembled tool surface: specs (for the API), handlers, and summaries."""

    specs: list[dict[str, Any]]
    handlers: dict[str, ToolHandler]
    summaries: dict[str, str]


def _to_openai_spec(tool: Tool) -> dict[str, Any]:
    """Render a tool as an OpenAI function-tool spec."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def build_tools(*tools: Tool) -> ToolRegistry:
    """Assemble a ``ToolRegistry`` from the given tools.

    Called with the single ``BashTool`` instance in normal operation. The split
    (specs vs handlers vs summaries) matches the harness so the prompt builder
    consumes summaries without special-casing any individual tool.
    """
    specs: list[dict[str, Any]] = []
    handlers: dict[str, ToolHandler] = {}
    summaries: dict[str, str] = {}
    for tool in tools:
        specs.append(_to_openai_spec(tool))
        handlers[tool.name] = tool.run
        # The prompt's Tools section shows a one-line summary: first line of the
        # full description (the rest is the model-facing detail, already in the
        # tool spec sent to the API).
        summaries[tool.name] = tool.description.split("\n", 1)[0].strip()
    return ToolRegistry(specs=specs, handlers=handlers, summaries=summaries)


def get_tool_summaries(registry: ToolRegistry) -> dict[str, str]:
    """Return the ``name -> one-line summary`` map for the prompt builder."""
    return registry.summaries
