"""bash - claw-zero's local shell executor.

Adapted from the harness ``ExecTool`` (``harness/tools/tools_shell.py``) by
re-pointing it from the VM RPC to a **local** ``asyncio`` subprocess running in
the agent's own working directory. Same shape: a single command in, clamped
timeout, middle-truncated ``stdout``/``stderr`` (head + tail preserved so exit
and error lines survive), and a structured ``{exit_code, stdout, stderr, ...}``
return.

This class now backs OpenAI's native local Shell tool. ``run`` preserves the
legacy function-tool shape for tests and internal callers; ``run_shell_call``
adapts the same executor to Responses ``shell_call_output`` items.

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


LOCAL_SHELL_DESCRIPTION = """\
Run shell commands on the local machine through OpenAI's native local Shell tool.

The shell is also how you touch the filesystem — there are no dedicated file \
tools:
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
- On expiry the command is killed and you get a timeout outcome instead of \
hanging. For a command that must run unattended, keep it short and deterministic \
— do not build tight polling loops.

Output:
- Large visible outputs may be limited by the Shell tool. Read large files in \
ranges with `sed -n` rather than dumping them whole.
- When a tool result might be cleared from your context later, write anything \
you need to remember into your memory files (see Memory)."""

BASH_DESCRIPTION = LOCAL_SHELL_DESCRIPTION


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

    name = "shell"

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
        return LOCAL_SHELL_DESCRIPTION

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
        return await self._run_command(command, timeout_seconds, max_output_chars=self.max_output_chars)

    async def run_shell_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a Responses ``shell_call`` action and return ``shell_call_output``."""
        call_id = params.get("call_id", "")
        commands = params.get("commands", [])
        if isinstance(commands, str):
            commands = [commands]
        commands = [cmd for cmd in commands if isinstance(cmd, str) and cmd.strip()]
        max_output_length = _positive_int(params.get("max_output_length"))
        timeout_seconds = _timeout_ms_to_seconds(params.get("timeout_ms"), self.default_timeout)

        output: list[dict[str, Any]] = []
        if not commands:
            output.append({
                "stdout": "",
                "stderr": "Error: shell_call action contained no commands.",
                "outcome": {"type": "exit", "exit_code": 1},
            })
        for command in commands:
            result = await self._run_command(command, timeout_seconds, max_output_chars=None)
            output.append(_to_shell_command_output(result))

        payload: dict[str, Any] = {
            "type": "shell_call_output",
            "call_id": call_id,
            "output": output,
        }
        if max_output_length is not None:
            payload["max_output_length"] = max_output_length
        return payload

    async def _run_command(
        self,
        command: str,
        timeout_seconds: int,
        *,
        max_output_chars: int | None,
    ) -> dict[str, Any]:
        """Run one command, optionally middle-truncating stdout/stderr."""
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
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=5)
            except (asyncio.TimeoutError, RuntimeError):
                stdout_b, stderr_b = b"", b""
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self._consume_cwd(cwd_path)
            stdout, out_trunc = _decode_and_maybe_truncate(stdout_b, max_output_chars)
            stderr, err_trunc = _decode_and_maybe_truncate(stderr_b, max_output_chars)
            return {
                "success": False,
                "status": "failed",
                "timed_out": True,
                "error": f"Error: command timed out after {timeout_seconds}s and was killed.",
                "stdout": stdout,
                "stderr": stderr,
                "truncated": out_trunc or err_trunc,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "cwd": self.cwd,
            }

        duration_ms = int((time.monotonic() - t0) * 1000)
        self._consume_cwd(cwd_path)

        stdout, out_trunc = _decode_and_maybe_truncate(stdout_b, max_output_chars)
        stderr, err_trunc = _decode_and_maybe_truncate(stderr_b, max_output_chars)
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


def _decode_and_maybe_truncate(data: bytes, cap: int | None) -> tuple[str, bool]:
    text = data.decode("utf-8", "replace")
    if cap is None:
        return text, False
    return _truncate_middle(text, cap)


def _positive_int(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return int(raw)


def _timeout_ms_to_seconds(raw: Any, default: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return default
    return _clamp_timeout(raw / 1000, default)


def _to_shell_command_output(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("timed_out"):
        outcome = {"type": "timeout"}
    else:
        exit_code = result.get("exit_code", 1)
        outcome = {"type": "exit", "exit_code": int(exit_code) if isinstance(exit_code, int) else 1}
    stderr = result.get("stderr", "")
    if result.get("error") and not stderr:
        stderr = result["error"]
    return {
        "stdout": result.get("stdout", ""),
        "stderr": stderr,
        "outcome": outcome,
    }
