"""Transcript — append-only JSONL log of an activation.

Trimmed port of the harness ``SessionManager`` JSONL writer
(``harness/session.py``). Keeps the three entry types that matter:

  - ``session``    — header: version, agent_id, run_number, model
  - ``message``    — one conversation message (role, content, optional usage,
                     optional stopReason), chained by ``parentId``
  - ``compaction`` — summary, firstKeptEntryId, tokensBefore

Dropped: cross-run ``state.json``, replay, and image entries (claw-zero has no
images). Each entry is one JSON line at ``<base_dir>/<agent_id>/transcript.jsonl``.
The run number is derived by counting existing ``session`` headers, so re-opening
the same file continues the run sequence.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Transcript:
    """Append-only JSONL transcript for one agent.

    Args:
        agent_id: Identifier scoping the transcript file.
        base_dir: Root directory (default ``claw_zero_state``). The transcript
            lands at ``<base_dir>/<agent_id>/transcript.jsonl``.
    """

    DEFAULT_BASE_DIR = "claw_zero_state"

    def __init__(self, agent_id: str = "claw-zero", base_dir: str | Path | None = None) -> None:
        self.agent_id = agent_id
        self.base_dir = Path(base_dir if base_dir is not None else self.DEFAULT_BASE_DIR)
        self._last_entry_id: str | None = None

    @property
    def agent_dir(self) -> Path:
        return self.base_dir / self.agent_id

    @property
    def path(self) -> Path:
        return self.agent_dir / "transcript.jsonl"

    def init_session(self, model: str = "") -> int:
        """Append a session header and return the derived run number."""
        run_number = self._count_session_headers() + 1
        self._append(
            "session",
            {"version": 1, "agent_id": self.agent_id, "run_number": run_number, "model": model},
            parent_id=None,
        )
        return run_number

    def append_message(
        self,
        role: str,
        content: str | list[dict[str, Any]],
        *,
        usage: dict[str, Any] | None = None,
        stop_reason: str | None = None,
    ) -> str:
        """Append a message entry. Returns the new entry id."""
        content_array = [{"type": "text", "text": content}] if isinstance(content, str) else content
        message: dict[str, Any] = {"role": role, "content": content_array}
        if usage is not None:
            message["usage"] = usage
        if stop_reason is not None:
            message["stopReason"] = stop_reason
        return self._append("message", {"message": message})

    def append_compaction(self, summary: str, first_kept_entry_id: str, tokens_before: int) -> str:
        """Append a compaction entry. Returns the new entry id."""
        return self._append(
            "compaction",
            {"summary": summary, "firstKeptEntryId": first_kept_entry_id, "tokensBefore": tokens_before},
        )

    @property
    def transcript_bytes(self) -> int:
        """Current on-disk size of the transcript file (0 if absent)."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    # -- internals --

    def _append(self, entry_type: str, data: dict[str, Any], parent_id: str | None = "__chain__") -> str:
        entry_id = _new_id(entry_type[:4])
        entry: dict[str, Any] = {
            "type": entry_type,
            "id": entry_id,
            "parentId": self._last_entry_id if parent_id == "__chain__" else parent_id,
            "timestamp": _now_iso(),
        }
        entry.update(data)
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        self._last_entry_id = entry_id
        return entry_id

    def _count_session_headers(self) -> int:
        if not self.path.exists():
            return 0
        count = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("type") == "session":
                    count += 1
            except json.JSONDecodeError:
                continue
        return count
