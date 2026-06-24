"""spawn_agent — add a new teammate to the running team at runtime.

A spawned agent is a full peer: its own ``Agent`` (memory, transcript, tools),
its own inbox on the shared ``MessageBus``, and its own outer-loop task. It is
**not** a subordinate — claw-zero is a flat mesh, so a spawned agent talks to
everyone (including its spawner) as an equal. Use it to bring a focused
collaborator online (a researcher, a reviewer) when the work warrants another
pair of hands.

This tool is bound to the ``Team``'s ``spawn`` callback (kept as an injected
callable to avoid a tool→team import cycle) and to the spawning agent's id. The
``Team`` owns the actual wiring and lifecycle; the tool validates and hands off.
After spawning, send the new teammate its first task with ``send_message`` (or
pass a ``brief`` here, delivered as its opening message).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

# spawn(new_id, model, brief, spawned_by) -> result dict. Supplied by Team.
SpawnFn = Callable[..., Awaitable[dict[str, Any]]]


SPAWN_AGENT_DESCRIPTION = """\
Bring a new teammate agent online, right now, as an equal peer on the team. The \
new agent gets its own memory, its own message inbox, and its own loop — it is \
not your subordinate; it can message anyone, including you.

Spawn one when the work genuinely benefits from another agent working in \
parallel or with a distinct focus (e.g. a 'researcher' digging while you build, \
a 'reviewer' to check your work). Don't spawn for trivial steps you can just do \
yourself, and don't spawn duplicates — reuse a teammate that already exists by \
messaging it.

Parameters:
- `id`: the new teammate's id/name (lowercase, short, unique on the team).
- `model`: optional LiteLLM model id; defaults to your own model.
- `brief`: optional opening task, delivered to the new agent as its first \
message (from you). Brief it like a colleague who just walked in: what to do, \
why, and what you've already learned — it starts with none of your context.

Returns the new id once it's running. It will reply to you (or whoever its \
brief concerns) on its own activations — do not poll."""


class SpawnAgentTool:
    """Create and start a new teammate agent on the team."""

    name = "spawn_agent"

    def __init__(self, spawn: SpawnFn, spawner_id: str) -> None:
        self._spawn = spawn
        self._spawner_id = spawner_id

    @property
    def description(self) -> str:
        return SPAWN_AGENT_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "New teammate id/name (lowercase, short, unique on the team).",
                },
                "model": {
                    "type": "string",
                    "description": "Optional LiteLLM model id. Defaults to your own model.",
                },
                "brief": {
                    "type": "string",
                    "description": (
                        "Optional opening task, delivered as the new agent's first "
                        "message (sent from you). Give it the context it needs to start."
                    ),
                },
            },
            "required": ["id"],
        }

    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        new_id = params.get("id")
        if not isinstance(new_id, str) or not new_id.strip():
            return {"success": False, "error": "'id' must be a non-empty string."}
        model = params.get("model")
        if model is not None and not isinstance(model, str):
            return {"success": False, "error": "'model' must be a string if given."}
        brief = params.get("brief")
        if brief is not None and not isinstance(brief, str):
            return {"success": False, "error": "'brief' must be a string if given."}

        return await self._spawn(
            new_id=new_id.strip(),
            model=model,
            brief=brief,
            spawned_by=self._spawner_id,
        )
