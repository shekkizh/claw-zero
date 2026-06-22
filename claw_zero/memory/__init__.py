"""Durable, file-backed memory — the keeper feature.

Two layers, ported from the ALE Claw harness:
  - append-only session log (``memory/session-NNN.md``) — the scratchpad
  - curated memory (``AGENT_MEMORY.md``) — distilled, full-overwrite knowledge

Plus the flush-before-compaction turn that writes durable memory before old
context is summarized away. The agent reads/writes memory **via bash** in the
main loop; the flush turn uses the store directly.
"""

from .store import MemoryStore
from .flush import maybe_flush_memory, run_memory_flush, should_run_memory_flush

__all__ = [
    "MemoryStore",
    "maybe_flush_memory",
    "run_memory_flush",
    "should_run_memory_flush",
]
