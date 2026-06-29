"""Runtime state persistence for reloadable claw-zero workers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNTIME_STATE_FILE = "runtime_state.json"
RELOAD_STATE_FILE = "reload_state.json"
TEAM_STATE_FILE = "team_state.json"
RELOAD_REQUESTED_EXIT_CODE = 75
RUNTIME_STATE_VERSION = 1
TEAM_STATE_VERSION = 1
DEFAULT_BASE_DIR = "claw_zero_state"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_roundtrip(value: Any) -> Any:
    """Return ``value`` as JSON primitives, failing if it is not serializable."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def state_path(agent_dir: Path) -> Path:
    return agent_dir / RUNTIME_STATE_FILE


def reload_state_path(agent_dir: Path) -> Path:
    return agent_dir / RELOAD_STATE_FILE


def team_state_path(base_dir: str | Path | None) -> Path:
    return Path(base_dir if base_dir is not None else DEFAULT_BASE_DIR) / TEAM_STATE_FILE


def load_runtime_state(agent_dir: Path) -> dict[str, Any] | None:
    path = state_path(agent_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("version") != RUNTIME_STATE_VERSION:
        return None
    return payload


def save_agent_state(agent: Any, *, reason: str) -> dict[str, Any]:
    session_log = None
    if agent.memory_store.current_session_path is not None:
        try:
            session_log = str(
                agent.memory_store.current_session_path.resolve().relative_to(
                    agent.memory_store.base_dir.resolve()
                )
            )
        except ValueError:
            session_log = str(agent.memory_store.current_session_path)

    saved_at = _now_iso()
    payload: dict[str, Any] = {
        "version": RUNTIME_STATE_VERSION,
        "saved_at": saved_at,
        "reason": reason,
        "agent_id": agent.agent_id,
        "model": agent.model,
        "messages": _json_roundtrip(agent.messages),
        "flush_state": {
            "compaction_count": agent.flush_state.compaction_count,
            "flushed_at_compaction_count": agent.flush_state.flushed_at_compaction_count,
        },
        "last_api_input_tokens": agent.last_api_input_tokens,
        "context_window": agent.context_window,
        "auto_compact_token_limit": agent.auto_compact_token_limit,
        "tool_output_token_limit": agent.tool_output_token_limit,
        "max_tool_result_chars": agent.max_tool_result_chars,
        "shell_cwd": agent._cwd(),
        "session_log": session_log,
        "transcript_last_entry_id": agent.transcript.last_entry_id,
    }
    _write_json(state_path(agent.memory_store.agent_dir), payload)
    return payload


def _read_reload_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("version") != 1:
        return None
    return payload


def pending_reload_continue(base_dir: str | Path | None) -> dict[str, Any] | None:
    """Return the newest reload_state needing a post-restart continue message.

    A reload request exits before the agent can finish its peer reply. On the
    next worker start, enqueueing a normal operator message with content
    ``"continue"`` lets the restored conversation proceed from the paired
    reload_harness tool result instead of waiting silently for manual input.
    """
    root = Path(base_dir if base_dir is not None else DEFAULT_BASE_DIR)
    if not root.exists():
        return None
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for path in root.glob(f"*/{RELOAD_STATE_FILE}"):
        payload = _read_reload_payload(path)
        if payload is None or payload.get("continue_enqueued_at"):
            continue
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            continue
        requested_at = payload.get("requested_at") if isinstance(payload.get("requested_at"), str) else ""
        candidates.append((requested_at, path, payload))
    if not candidates:
        return None
    _requested_at, path, payload = sorted(candidates, key=lambda item: (item[0], str(item[1])))[-1]
    result = dict(payload)
    result["path"] = str(path)
    return result


def mark_reload_continue_enqueued(
    path: str | Path,
    *,
    sender: str,
    recipient: str,
    content: str = "continue",
) -> dict[str, Any] | None:
    """Mark a reload_state as having had its synthetic continue message queued."""
    reload_path = Path(path)
    payload = _read_reload_payload(reload_path)
    if payload is None:
        return None
    payload["continue_enqueued_at"] = _now_iso()
    payload["continue_message"] = {
        "sender": sender,
        "recipient": recipient,
        "content": content,
    }
    _write_json(reload_path, payload)
    return payload


def load_team_state(base_dir: str | Path | None) -> dict[str, Any] | None:
    path = team_state_path(base_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("version") != TEAM_STATE_VERSION:
        return None
    return payload


def write_team_state(base_dir: str | Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload["version"] = TEAM_STATE_VERSION
    payload["saved_at"] = _now_iso()
    _write_json(team_state_path(base_dir), payload)
    return payload


def write_reload_state(agent: Any, request: Any, *, state_payload: dict[str, Any]) -> dict[str, Any]:
    from .source_identity import collect_source_identity

    payload = {
        "version": 1,
        "requested_at": _now_iso(),
        "agent_id": agent.agent_id,
        "reason": request.reason,
        "tests_run": request.tests_run,
        "summary": request.summary,
        "source_root": str(Path.cwd().resolve()),
        "source_identity": collect_source_identity(),
        "last_completed_state_save_id": state_payload.get("saved_at"),
        "exit_code": RELOAD_REQUESTED_EXIT_CODE,
    }
    _write_json(reload_state_path(agent.memory_store.agent_dir), payload)
    return payload
