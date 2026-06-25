"""spawn_agent — add a new teammate to the running team at runtime.

A spawned agent is a full peer, built from the same setup as its spawner: the
same tools and operating loop, just its own ``Agent`` (memory, transcript), its
own inbox on the shared ``MessageBus``, and its own outer-loop task. It is **not**
a specialized worker and **not** a subordinate — claw-zero is a flat mesh, so a
spawned agent talks to everyone (including its spawner) as an equal. What makes
it a distinct teammate is its ``id`` (its name — its identity on the bus, how
messages route to and from it) and the ``brief`` that points it at a task; any
"role" is just that focus, not a type. Use it to bring another pair of hands
online (research while you build, a second read on your work) when warranted.

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
Bring a new teammate online, right now, as an equal peer on the team. The new \
agent is a full copy of your own setup — same tools, same operating loop, same \
capabilities — running on its own. It is not a specialized worker and not your \
subordinate: it starts where you started, gets its own memory and message \
inbox, and can message anyone, including you. Two things make it a distinct \
teammate: its name — its identity on the bus, how everyone addresses it — and \
the initial brief you hand it, which is the only thing that points it at getting started. Note that the brief can be just what you are planning/doing when you are spawning it.

Spawn one when the work genuinely benefits from another agent in parallel — a \
second pair of hands to research while you do your own thing, or to review what you've \
done. Don't spawn for trivial steps you can just do yourself, and don't spawn \
duplicates — reuse a teammate that already exists by messaging it.

Parameters:
- `id`: the new teammate's name — its identity on the team, used to route \
messages to and from it. Lowercase, short, unique on the team; pick something \
that hints at the focus you're giving it (e.g. `researcher`, `reviewer`).
- `brief`: optional opening task, delivered to the new agent as its first \
message (from you). This is the initial context it has — it starts fresh with none \
of yours. Brief it like a colleague who just walked in: what you are doing and need help with, why, and \
what you've already learned or ruled out.

Returns the new name once it's running. It will reply to you (or whoever its \
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
                    "description": (
                        "New teammate's name — its identity on the team, used to route "
                        "messages. Lowercase, short, unique; hint at its focus (e.g. "
                        "'researcher', 'reviewer')."
                    ),
                },
                "brief": {
                    "type": "string",
                    "description": (
                        "Optional opening task, delivered as the new agent's first "
                        "message (sent from you). Its only context — brief it like a "
                        "colleague who just walked in: what to do, why, what you've learned."
                    ),
                },
            },
            "required": ["id"],
        }

    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        new_id = params.get("id")
        if not isinstance(new_id, str) or not new_id.strip():
            return {"success": False, "error": "'id' must be a non-empty string."}
        brief = params.get("brief")
        if brief is not None and not isinstance(brief, str):
            return {"success": False, "error": "'brief' must be a string if given."}

        return await self._spawn(
            new_id=new_id.strip(),
            model=None,
            brief=brief,
            spawned_by=self._spawner_id,
        )
