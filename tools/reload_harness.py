"""reload_harness — request a clean worker restart from a process boundary."""

from __future__ import annotations

from typing import Any


class ReloadRequested(RuntimeError):
    """Signal that the worker should save state and exit for reload."""

    def __init__(self, *, reason: str, tests_run: str = "", summary: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.tests_run = tests_run
        self.summary = summary
        self.agent_id = ""

    def tool_result(self) -> dict[str, Any]:
        return {
            "success": True,
            "reload_requested": True,
            "reason": self.reason,
            "tests_run": self.tests_run,
            "summary": self.summary,
            "note": "Runtime state was saved and the worker will exit for parent-process restart.",
        }


RELOAD_HARNESS_DESCRIPTION = """\
Request a clean restart of the claw-zero worker so source edits take effect.

Use this only after changing harness source files and running the verification \
that makes sense for the change. The source tree is shared by the whole worker: \
if one teammate improves the harness, the restarted code applies to all agents \
in that worker. It does not finish the current peer task by itself: it saves \
runtime state, asks the worker to exit with the reload code, and lets the \
built-in parent process start a fresh interpreter from the source tree. After
restart, the worker queues one normal operator message with content `continue` so
the requesting agent can proceed from the saved tool result.

Report verification honestly. If no tests were run, say that in `tests_run`."""


class ReloadHarnessTool:
    """Raise ``ReloadRequested`` after validating the model's reload request."""

    name = "reload_harness"

    @property
    def description(self) -> str:
        return RELOAD_HARNESS_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why a worker restart is needed now.",
                },
                "tests_run": {
                    "type": "string",
                    "description": "Verification performed before reload, or 'not run' with the reason.",
                },
                "summary": {
                    "type": "string",
                    "description": "Short summary of the source changes that should be picked up.",
                },
            },
            "required": ["reason"],
        }

    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        reason = params.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return {"success": False, "error": "'reason' must be a non-empty string."}
        tests_run = params.get("tests_run", "")
        summary = params.get("summary", "")
        if tests_run is not None and not isinstance(tests_run, str):
            return {"success": False, "error": "'tests_run' must be a string if given."}
        if summary is not None and not isinstance(summary, str):
            return {"success": False, "error": "'summary' must be a string if given."}
        raise ReloadRequested(
            reason=reason.strip(),
            tests_run=(tests_run or "").strip(),
            summary=(summary or "").strip(),
        )
