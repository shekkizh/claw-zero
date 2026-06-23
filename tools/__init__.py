"""Tools — exactly one: ``bash`` (client-side, local subprocess).

``bash`` is also the file tool: read with ``cat``/``sed -n``, search with
``grep -rn``/``rg``, find with ``find``, edit with ``sed``/``python -c``. There
are no dedicated read/write/edit/grep/glob tools, no web search, and no
permission gate.
"""

from .bash import BashTool
from .registry import build_tools, get_tool_summaries

__all__ = ["BashTool", "build_tools", "get_tool_summaries"]
