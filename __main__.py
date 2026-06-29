"""Entry point — ``python -m claw_zero``.

Normal invocation starts a small stable parent process, which launches a worker
from the current source tree. The worker wires up config → Team (bus + one or
more agents) → human StdioPeer (+ optional self-tick) → memory → prompt → tools,
then runs the team until stdin closes. Agents can call ``reload_harness`` after
source edits; the worker saves state and exits, and the parent starts a fresh
interpreter. API keys are read from the environment by the OpenAI SDK - never
from config or argv.

Usage:
    OPENAI_API_KEY=... python -m claw_zero
    python -m claw_zero --model gpt-5.5 --tick-seconds 60
    python -m claw_zero --agents planner,coder,reviewer   # a team of four
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

from .config import ClawZeroConfig
from .messaging.mailbox import Message
from .messaging.peer import StdioPeer
from .runtime_state import (
    RELOAD_REQUESTED_EXIT_CODE,
    mark_reload_continue_enqueued,
    pending_reload_continue,
)
from .source_identity import collect_source_identity, format_source_identity
from .supervisor import supervise_command
from .team import Team
from .tools.reload_harness import ReloadRequested


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _enqueue_reload_continue_if_needed(team: Team, config: ClawZeroConfig) -> bool:
    """After a reload, queue a normal operator ``continue`` message once.

    ``reload_harness`` exits before the agent can produce its final peer reply.
    The restarted worker has the paired tool result in runtime state, but it
    needs a fresh inbound message to continue the activation loop. The policy is
    intentionally simple: add a message from the operator with content
    ``continue``.
    """
    pending = pending_reload_continue(config.base_dir)
    if pending is None:
        return False
    agent_id = pending.get("agent_id")
    path = pending.get("path")
    if not isinstance(agent_id, str) or agent_id not in team.agent_ids:
        return False
    if not isinstance(path, str) or not path:
        return False
    content = "continue"
    delivered = await team.bus.route(
        Message(
            sender=config.operator_id,
            recipient=agent_id,
            content=content,
            kind="message",
            ts=_now_iso(),
        )
    )
    if delivered:
        mark_reload_continue_enqueued(
            path,
            sender=config.operator_id,
            recipient=agent_id,
            content=content,
        )
    return delivered


def _load_agents_md() -> str | None:
    """Load the packaged ``AGENTS.md`` (the persistent operating doc)."""
    path = Path(__file__).with_name("AGENTS.md")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _parse_args(argv: list[str] | None = None) -> ClawZeroConfig:
    parser = argparse.ArgumentParser(prog="claw-zero", description="A self-owned, long-running agent loop (single agent or a team).")
    parser.add_argument("--model", default=ClawZeroConfig.model, help="OpenAI model id (default: %(default)s)")
    parser.add_argument("--agent-id", default=ClawZeroConfig.agent_id, help="This agent's id (default: %(default)s)")
    parser.add_argument(
        "--operator-id", default=ClawZeroConfig.operator_id,
        help="Your participant name on the bus; agents address you by it (default: %(default)s)",
    )
    parser.add_argument(
        "--agents", default="",
        help="Comma-separated extra teammate ids to launch at startup (a flat peer mesh). Default: none (single agent).",
    )
    parser.add_argument(
        "--no-spawn", action="store_true",
        help="Disallow runtime spawning of new teammates (drops the spawn_agent tool).",
    )
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--resume-runtime-state",
        action="store_true",
        help="Resume from runtime_state.json if present (normally used by the supervisor).",
    )
    parser.add_argument(
        "--max-reloads",
        type=int,
        default=ClawZeroConfig.max_reloads,
        help="Maximum reload restarts in one run (default: %(default)s).",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tick-seconds", type=float, default=None, help="Self-tick interval, applied to every agent (default: off)")
    parser.add_argument("--base-dir", default=None, help="State root (default: claw_zero_state)")
    parser.add_argument(
        "--auto-compact-token-limit", type=int, default=None,
        help="Prompt-token count that triggers compaction (default: Codex-style model-derived limit)",
    )
    parser.add_argument(
        "--tool-output-token-limit", type=int, default=ClawZeroConfig.tool_output_token_limit,
        help="Approximate per-tool-output token cap (default: %(default)s)",
    )
    parser.add_argument(
        "--compaction-threshold", type=float, default=None,
        help="Compatibility alias: context fraction that triggers compaction when no token limit is set",
    )
    parser.add_argument(
        "--max-tool-result-chars", type=int, default=None,
        help="Compatibility alias: per-tool-result char cap",
    )
    parser.add_argument("--context-window-tokens", type=int, default=None, help="Override resolved context window")
    args = parser.parse_args(argv)
    roster = [a.strip() for a in args.agents.split(",") if a.strip()]
    return ClawZeroConfig(
        model=args.model,
        agent_id=args.agent_id,
        operator_id=args.operator_id,
        agents=roster,
        allow_spawn=not args.no_spawn,
        reload_enabled=True,
        resume_runtime_state=args.resume_runtime_state,
        supervise=not args.worker,
        worker=args.worker,
        max_reloads=args.max_reloads,
        tick_seconds=args.tick_seconds,
        base_dir=args.base_dir,
        auto_compact_token_limit=args.auto_compact_token_limit,
        tool_output_token_limit=args.tool_output_token_limit,
        compaction_threshold=args.compaction_threshold,
        max_tool_result_chars=args.max_tool_result_chars,
        context_window_tokens=args.context_window_tokens,
    )


async def _run(config: ClawZeroConfig, argv: list[str] | None = None) -> None:
    team = Team(
        config,
        agents_md=_load_agents_md(),
        allow_spawn=config.allow_spawn,
        allow_reload=config.reload_enabled,
        resume_runtime_state=config.resume_runtime_state,
    )

    # The primary agent plus any roster teammates. Your typed lines reach the
    # primary by default; address any other agent by name (`@name` / `name:`).
    team.add_agent(config.agent_id, config.model)
    for name in config.agents:
        team.add_agent(name, config.model)
    if config.resume_runtime_state:
        team.restore_saved_agents()

    operator = StdioPeer(peer_id=config.operator_id, default_recipient=config.agent_id)
    team.add_peer(operator)

    identity = collect_source_identity(
        source_root=Path(__file__).resolve().parent,
        argv=argv or [],
        model=config.model,
        state_dir=config.base_dir or "claw_zero_state",
        worker=config.worker,
    )
    print(f"claw-zero {format_source_identity(identity)}", flush=True)
    team.record_worker_start(identity)
    await _enqueue_reload_continue_if_needed(team, config)

    roster = team.agent_ids
    if len(roster) == 1:
        who = f"agent [{config.agent_id}]"
    else:
        who = f"team of {len(roster)} [{', '.join(roster)}]"
    print(
        f"claw-zero {who} online — model {config.model}, "
        f"context window {team.context_window_of(config.agent_id):,} tokens. "
        f"You are '{config.operator_id}'. "
        + (
            "Type a message; prefix with `@name` or `name:` to address one agent. "
            if len(roster) > 1 else "Type a message. "
        )
        + "Ctrl-D or Ctrl-C to exit.",
        flush=True,
    )

    await team.run_until_eof(operator)


def _worker_argv(raw_argv: list[str]) -> list[str]:
    child = [arg for arg in raw_argv if arg != "--supervise"]
    if "--worker" not in child:
        child.append("--worker")
    if "--resume-runtime-state" not in child:
        child.append("--resume-runtime-state")
    return child


async def _supervise(raw_argv: list[str], max_reloads: int) -> int:
    source_root = Path(__file__).resolve().parent
    child_argv = _worker_argv(raw_argv)
    return await supervise_command(
        [sys.executable, "-m", "claw_zero", *child_argv],
        cwd=source_root,
        env=os.environ.copy(),
        max_reloads=max_reloads,
    )


def main(argv: list[str] | None = None) -> None:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    config = _parse_args(raw_argv)
    try:
        if not config.worker:
            raise SystemExit(asyncio.run(_supervise(raw_argv, config.max_reloads)))
        asyncio.run(_run(config, raw_argv))
    except ReloadRequested as exc:
        print(f"\nclaw-zero: reload requested: {exc.reason}", flush=True)
        raise SystemExit(RELOAD_REQUESTED_EXIT_CODE) from exc
    except KeyboardInterrupt:
        print("\nclaw-zero: interrupted, shutting down.", flush=True)


if __name__ == "__main__":
    main()
