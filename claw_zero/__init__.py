"""claw-zero — a minimal, non-user-facing, self-owned, long-running agent loop.

Defining idea: **humans and agents are equal units/operators.** The agent does
not special-case "the user." It only ever (1) receives a *message* addressed to
it — from a human peer, another agent, or a self-tick — and (2) sends a *message*
to some peer. An activation ends when the agent delivers a message; there is no
"DONE" terminal state and no user-facing mode. The outer loop then waits for the
next message and goes again, forever.

This package is intentionally self-contained: it ports the durable pieces of the
ALE Claw harness (``context/``, ``memory/``, the ``model/`` thinking + cache
layers) with **no** dependency on the cua SDK, computer-use, GUI, images, or
subagent delegation. See ``PORTING.md`` for the per-source KEEP/PORT/DROP map and
``README.md`` for the quickstart and architecture.

Submodules are imported lazily (importing ``claw_zero`` does not pull in
``litellm``) — reach for ``claw_zero.llm``, ``claw_zero.outer_loop``, etc.
directly.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
