"""Stable parent process for reloadable claw-zero workers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Mapping

from .runtime_state import RELOAD_REQUESTED_EXIT_CODE


async def supervise_command(
    command: list[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str] | None = None,
    max_reloads: int = 5,
    reload_exit_code: int = RELOAD_REQUESTED_EXIT_CODE,
) -> int:
    """Run ``command`` and restart only when it exits with ``reload_exit_code``."""
    reloads = 0
    while True:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
        )
        rc = await proc.wait()
        if rc != reload_exit_code:
            print(f"claw-zero supervisor: worker exited with code {rc}.", flush=True)
            return rc
        if reloads >= max_reloads:
            print(
                f"claw-zero supervisor: reload limit reached ({max_reloads}); not restarting.",
                flush=True,
            )
            return rc
        reloads += 1
        print(f"claw-zero supervisor: reload requested; restarting worker #{reloads}.", flush=True)
