"""Source-tree identity captured at worker start and reload request time."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .reload_marker import SOURCE_RELOAD_MARKER


def collect_source_identity(
    *,
    source_root: str | Path | None = None,
    argv: list[str] | None = None,
    model: str = "",
    state_dir: str = "",
    worker: bool = False,
) -> dict[str, Any]:
    root = Path(source_root or Path(__file__).resolve().parent).resolve()
    return {
        "source_root": str(root),
        "source_reload_marker": SOURCE_RELOAD_MARKER,
        "git_commit": _git(root, "rev-parse", "--short", "HEAD"),
        "git_dirty": _git_dirty(root),
        "argv": list(argv or []),
        "model": model,
        "state_dir": state_dir,
        "worker": worker,
    }


def format_source_identity(identity: dict[str, Any]) -> str:
    dirty = identity.get("git_dirty")
    dirty_text = "dirty" if dirty is True else "clean" if dirty is False else "unknown"
    return (
        "worker source: "
        f"root={identity.get('source_root', '')}; "
        f"git={identity.get('git_commit') or 'unknown'}; "
        f"status={dirty_text}; "
        f"marker={identity.get('source_reload_marker', '')}; "
        f"state={identity.get('state_dir', '')}"
    )


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _git_dirty(root: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())
