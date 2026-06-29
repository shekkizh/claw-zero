"""Tools - hosted web search, local Shell, and reload.

``shell`` is also the file tool: read with ``cat``/``sed -n``, search with
``grep -rn``/``rg``, find with ``find``, edit with ``sed``/``python -c``. There
are no dedicated read/write/edit/grep/glob tools and no permission gate.
"""

from .bash import BashTool
from .reload_harness import ReloadHarnessTool
from .registry import build_tools, get_tool_summaries

__all__ = [
    "BashTool",
    "ReloadHarnessTool",
    "build_tools",
    "get_tool_summaries",
]
