"""Entry point — ``python -m claw_zero``.

Wires up config → Team (bus + one or more agents) → human StdioPeer (+ optional
self-tick) → memory → prompt → tools, then runs the team until stdin closes. The
human is just a peer over stdio; agents are equal peers on the same bus. API keys
are read from the environment by the Cerebras SDK - never from config or argv.

Usage:
    CEREBRAS_API_KEY=... python -m claw_zero
    python -m claw_zero --model gpt-oss-120b --tick-seconds 60
    python -m claw_zero --agents planner,coder,reviewer   # a team of four
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import ClawZeroConfig
from .messaging.peer import StdioPeer
from .team import Team


def _load_agents_md() -> str | None:
    """Load the packaged ``AGENTS.md`` (the persistent operating doc)."""
    path = Path(__file__).with_name("AGENTS.md")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _parse_args(argv: list[str] | None = None) -> ClawZeroConfig:
    parser = argparse.ArgumentParser(prog="claw-zero", description="A self-owned, long-running agent loop (single agent or a team).")
    parser.add_argument("--model", default=ClawZeroConfig.model, help="Cerebras model id (default: %(default)s)")
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
        tick_seconds=args.tick_seconds,
        base_dir=args.base_dir,
        auto_compact_token_limit=args.auto_compact_token_limit,
        tool_output_token_limit=args.tool_output_token_limit,
        compaction_threshold=args.compaction_threshold,
        max_tool_result_chars=args.max_tool_result_chars,
        context_window_tokens=args.context_window_tokens,
    )


async def _run(config: ClawZeroConfig) -> None:
    team = Team(config, agents_md=_load_agents_md(), allow_spawn=config.allow_spawn)

    # The primary agent plus any roster teammates. Your typed lines reach the
    # primary by default; address any other agent by name (`@name` / `name:`).
    team.add_agent(config.agent_id, config.model)
    for name in config.agents:
        team.add_agent(name, config.model)

    operator = StdioPeer(peer_id=config.operator_id, default_recipient=config.agent_id)
    team.add_peer(operator)

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


def main(argv: list[str] | None = None) -> None:
    config = _parse_args(argv)
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        print("\nclaw-zero: interrupted, shutting down.", flush=True)


if __name__ == "__main__":
    main()
