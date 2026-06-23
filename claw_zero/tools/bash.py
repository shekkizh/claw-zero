"""bash — claw-zero's single tool: a client-side, local shell command.

Adapted from the harness ``ExecTool`` (``harness/tools/tools_shell.py``) by
re-pointing it from the VM RPC to a **local** ``asyncio`` subprocess running in
the agent's own working directory. Same shape: a single command in, clamped
timeout, middle-truncated ``stdout``/``stderr`` (head + tail preserved so exit
and error lines survive), and a structured ``{exit_code, stdout, stderr, ...}``
return.

``bash`` is the *only* tool, and it is also the file tool: read with
``cat``/``sed -n``, search with ``grep -rn``/``rg``, find with ``find``, edit
with ``sed``/``python -c``, write with redirection or ``python -c``. There are no
dedicated read/write/edit/grep/glob tools, no web search, and no permission gate.

The working directory **persists** between calls (a ``cd`` in one call is seen by
the next), but shell state does **not** — each call is a fresh ``/bin/sh``, so
exported variables, shell functions, and aliases do not carry over. Inline what
you need (``FOO=bar some_cmd``) within a single command.
"""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import time
from typing import Any


# Timeout bounds (seconds). Mirrors the harness exec clamp, widened to match
# Claude Code's Bash ceiling for long local builds/tests.
DEFAULT_TIMEOUT_SECONDS = 120
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 600

# Per-stream output cap (chars). Default matches config.max_tool_result_chars.
DEFAULT_MAX_OUTPUT_CHARS = 16_000


BASH_DESCRIPTION = """\
Executes a single shell command on the local machine and returns its \
stdout, stderr, and exit_code. The command runs in a fresh `/bin/sh -c`.

This is your ONLY tool, so it is also how you touch the filesystem — there are \
no dedicated file tools:
- Read a file: `cat path` or `sed -n '1,40p' path` (a line range).
- Search contents: `grep -rn "pattern" .` (or `rg "pattern"` if ripgrep is present).
- Find files: `find . -name '*.py'`.
- Edit in place: `sed -i '' 's/old/new/' path` (BSD/macOS) or a `python -c` script.
- Write a file: `python - <<'PY' ...` / `cat > path <<'EOF' ... EOF` / redirection.
- Inspect state: `ls -la`, `pwd`, `git status`, `python -c '...'`.

Working directory and shell state:
- The working directory PERSISTS between calls: a `cd subdir` in one call is in \
effect for the next call. Use this to navigate.
- Shell state does NOT persist: each call is a new shell, so exported variables, \
functions, and aliases from a previous call are gone. Inline what you need in a \
single command (e.g. `FOO=bar; some_cmd "$FOO"`).

Timeout:
- `timeout` is in SECONDS (default 120, min 1, max 600). On expiry the command \
is killed and you get a timeout error instead of hanging. For a command that \
must run unattended, keep it short and deterministic — do not build tight \
polling loops.

Output:
- Each of stdout/stderr is middle-truncated past ~16000 chars (head and tail \
are kept, so the final exit/error lines stay visible). Read large files in \
ranges with `sed -n` rather than dumping them whole.
- When a tool result might be cleared from your context later, write anything \
you need to remember into your memory files (see Memory)."""


def _clamp_timeout(raw: Any, default: int) -> int:
    """Clamp a user-supplied timeout (seconds) to ``[MIN, MAX]``; fall back to default."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return default
    return max(MIN_TIMEOUT_SECONDS, min(int(raw), MAX_TIMEOUT_SECONDS))


def _truncate_middle(s: str, cap: int) -> tuple[str, bool]:
    """Keep head + tail halves of ``s`` if it exceeds ``cap`` chars.

    Returns ``(text, was_truncated)``. Ported from the harness exec tool — the
    final exit/error lines stay visible because the tail is preserved.
    """
    if cap <= 0 or len(s) <= cap:
        return s, False
    half = cap // 2
    head = s[:half]
    tail = s[-(cap - half):] if (cap - half) > 0 else ""
    omitted = len(s) - len(head) - len(tail)
    marker = f"\n\n[... output truncated: {omitted} chars omitted ...]\n\n"
    return head + marker + tail, True


class BashTool:
    """Run a single local shell command, with a persistent working directory.

    Args:
        cwd: Starting working directory. Defaults to the process cwd. Updated
            in place when a command changes directory (``cd``), so the next call
            starts where the last one ended.
        max_output_chars: Per-stream truncation cap.
        default_timeout: Default per-call timeout in seconds.
    """

    name = "bash"

    def __init__(
        self,
        cwd: str | None = None,
        *,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        default_timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.max_output_chars = max_output_chars
        self.default_timeout = _clamp_timeout(default_timeout, DEFAULT_TIMEOUT_SECONDS)

    @property
    def description(self) -> str:
        return BASH_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run in /bin/sh.",
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        f"Timeout in seconds (default {DEFAULT_TIMEOUT_SECONDS}, "
                        f"min {MIN_TIMEOUT_SECONDS}, max {MAX_TIMEOUT_SECONDS}). "
                        "On expiry the command is killed and a timeout error is returned."
                    ),
                },
            },
            "required": ["command"],
        }

    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        """Execute ``params['command']`` locally and return a structured result."""
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            return {"success": False, "error": "Error: 'command' must be a non-empty string."}
        timeout_seconds = _clamp_timeout(params.get("timeout"), self.default_timeout)

        # Capture the post-command working directory via a temp file so a `cd`
        # inside the command persists to the next call without polluting stdout.
        # The capture runs in the SAME shell as the command, after it, guarded so
        # it always records PWD regardless of the command's exit code.
        cwd_fd, cwd_path = tempfile.mkstemp(prefix="claw_zero_cwd_")
        os.close(cwd_fd)
        wrapped = f"{command}\n__claw_rc=$?\nprintf '%s' \"$PWD\" > {_shquote(cwd_path)}\nexit $__claw_rc\n"

        t0 = time.monotonic()
        try:
            # start_new_session=True puts the shell in its own process group so a
            # timeout can kill the WHOLE tree (the shell and any children it
            # forked, e.g. `sleep`), not just the shell — otherwise children are
            # orphaned and the "command is killed" promise would be a lie.
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                wrapped,
                cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            os.unlink(cwd_path)
            return {"success": False, "error": f"Error: failed to launch shell: {e}"}

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            _kill_process_group(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self._consume_cwd(cwd_path)
            return {
                "success": False,
                "status": "failed",
                "timed_out": True,
                "error": f"Error: command timed out after {timeout_seconds}s and was killed.",
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "cwd": self.cwd,
            }

        duration_ms = int((time.monotonic() - t0) * 1000)
        self._consume_cwd(cwd_path)

        stdout, out_trunc = _truncate_middle(stdout_b.decode("utf-8", "replace"), self.max_output_chars)
        stderr, err_trunc = _truncate_middle(stderr_b.decode("utf-8", "replace"), self.max_output_chars)
        exit_code = proc.returncode if proc.returncode is not None else -1

        return {
            "success": True,
            "status": "completed" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": out_trunc or err_trunc,
            "duration_ms": duration_ms,
            "cwd": self.cwd,
        }

    def _consume_cwd(self, cwd_path: str) -> None:
        """Read the captured PWD, update ``self.cwd``, and remove the temp file."""
        try:
            captured = ""
            try:
                with open(cwd_path, "r", encoding="utf-8") as f:
                    captured = f.read().strip()
            finally:
                if os.path.exists(cwd_path):
                    os.unlink(cwd_path)
            if captured and os.path.isdir(captured):
                self.cwd = captured
        except OSError:
            pass


def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """SIGKILL the subprocess's whole process group (shell + forked children).

    Falls back to killing just the process if the group lookup fails (e.g. the
    process already exited).
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _shquote(path: str) -> str:
    """Single-quote a path for safe interpolation into an /bin/sh command."""
    return "'" + path.replace("'", "'\\''") + "'"
