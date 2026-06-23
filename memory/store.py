"""MemoryStore — durable, file-backed memory for claw-zero.

Ported from ``harness/memory/memory.py`` with the cua ``BaseTool`` memory tools
stripped (claw-zero reaches memory via bash, not a tool). Layout, agent-scoped:

    claw_zero_state/<agent_id>/
    ├── AGENT_MEMORY.md            # curated, full-overwrite knowledge
    └── memory/
        ├── session-001.md         # append-only session log
        ├── session-002.md
        └── ...

The path-traversal guard uses ``Path.is_relative_to`` (component-wise), not a
string prefix — a string prefix would accept a sibling like ``<base>-evil`` whose
path string starts with ``<base>``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


class MemoryStore:
    """Agent-scoped persistent memory (curated file + append-only session logs).

    Args:
        agent_id: Identifier scoping the workspace (one dir per agent).
        base_dir: Root for all agent workspaces (default ``claw_zero_state``).
    """

    CURATED_FILE = "AGENT_MEMORY.md"
    MEMORY_SUBDIR = "memory"
    DEFAULT_BASE_DIR = "claw_zero_state"

    def __init__(self, agent_id: str = "claw-zero", base_dir: str | Path | None = None) -> None:
        self.agent_id = agent_id
        self.base_dir = Path(base_dir if base_dir is not None else self.DEFAULT_BASE_DIR)
        self._current_session_path: Path | None = None

    @property
    def agent_dir(self) -> Path:
        """``base_dir/<agent_id>`` — this agent's workspace root."""
        return self.base_dir / self.agent_id

    @property
    def memory_dir(self) -> Path:
        """``agent_dir/memory`` — the session-log directory."""
        return self.agent_dir / self.MEMORY_SUBDIR

    @property
    def current_session_path(self) -> Path | None:
        """Path to the session log created by ``init_session`` (or None)."""
        return self._current_session_path

    def init_session(self) -> str:
        """Create the next sequential session log file.

        Returns the path relative to ``base_dir`` (e.g.
        ``claw-zero/memory/session-001.md``).
        """
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        next_num = 1
        for f in sorted(self.memory_dir.glob("session-*.md")):
            match = re.match(r"session-(\d+)\.md$", f.name)
            if match:
                next_num = max(next_num, int(match.group(1)) + 1)

        session_file = self.memory_dir / f"session-{next_num:03d}.md"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        session_file.write_text(f"# Session {next_num:03d} — {timestamp}\n\n", encoding="utf-8")
        self._current_session_path = session_file
        return str(session_file.relative_to(self.base_dir))

    def append_session(self, text: str) -> str:
        """Append a timestamped entry to the current session log.

        Raises ``RuntimeError`` if ``init_session`` has not been called.
        """
        if self._current_session_path is None:
            raise RuntimeError("init_session() must be called before append_session()")
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with open(self._current_session_path, "a", encoding="utf-8") as f:
            f.write(f"\n[{timestamp}] {text}\n")
        return str(self._current_session_path.relative_to(self.base_dir))

    def write_curated(self, content: str) -> None:
        """Overwrite ``AGENT_MEMORY.md`` in full. Creates the dir if absent."""
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_dir / self.CURATED_FILE).write_text(content, encoding="utf-8")

    def read_curated(self) -> str:
        """Read ``AGENT_MEMORY.md``. Returns ``""`` if missing/unreadable."""
        path = self.agent_dir / self.CURATED_FILE
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def read_file(self, relative_path: str, start: int = 1, end: int | None = None) -> str:
        """Read a memory file (within the workspace) with an optional line range.

        Args:
            relative_path: Path relative to ``base_dir`` (e.g.
                ``claw-zero/memory/session-001.md``).
            start: 1-based start line (default 1).
            end: 1-based inclusive end line (default: to EOF).

        Returns the content, or ``""`` if missing. Raises ``ValueError`` on a
        traversal attempt outside ``base_dir``.
        """
        resolved = (self.base_dir / relative_path).resolve()
        if not resolved.is_relative_to(self.base_dir.resolve()):
            raise ValueError("path traversal is not allowed; use a relative path within memory")
        if not resolved.exists():
            return ""
        try:
            lines = resolved.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeDecodeError):
            return ""
        start_idx = max(0, start - 1)
        end_idx = min(len(lines), end) if end is not None else len(lines)
        return "".join(lines[start_idx:end_idx])

    def list_session_files(self) -> list[str]:
        """Return sorted ``session-NNN.md`` paths relative to ``base_dir``."""
        if not self.memory_dir.exists():
            return []
        return [str(f.relative_to(self.base_dir)) for f in sorted(self.memory_dir.glob("session-*.md"))]
