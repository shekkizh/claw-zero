"""Entry point — ``python -m claw_zero``.

Wires up config → mailbox → StdioPeer (+ optional self-tick) → memory store →
prompt → tools → outer loop, then runs the self-owned loop forever. The human is
just a peer over stdio. API keys are read from the environment by litellm — never
from config or argv.

Usage:
    OPENAI_API_KEY=... python -m claw_zero
    python -m claw_zero --model anthropic/claude-opus-4-8 --tick-seconds 60
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import ClawZeroConfig
from .messaging.mailbox import Mailbox
from .messaging.peer import StdioPeer, tick_source
from .outer_loop import Agent, run


def _load_agents_md() -> str | None:
    """Load the packaged ``AGENTS.md`` (the persistent operating doc)."""
    path = Path(__file__).with_name("AGENTS.md")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _parse_args(argv: list[str] | None = None) -> ClawZeroConfig:
    parser = argparse.ArgumentParser(prog="claw-zero", description="A self-owned, long-running agent loop.")
    parser.add_argument("--model", default=ClawZeroConfig.model, help="LiteLLM model id (default: %(default)s)")
    parser.add_argument("--agent-id", default=ClawZeroConfig.agent_id, help="This agent's id (default: %(default)s)")
    parser.add_argument("--tick-seconds", type=float, default=None, help="Self-tick interval (default: off)")
    parser.add_argument("--base-dir", default=None, help="State root (default: claw_zero_state)")
    parser.add_argument(
        "--compaction-threshold", type=float, default=ClawZeroConfig.compaction_threshold,
        help="Context fraction that triggers compaction (default: %(default)s)",
    )
    parser.add_argument(
        "--max-tool-result-chars", type=int, default=ClawZeroConfig.max_tool_result_chars,
        help="Per-tool-result char cap (default: %(default)s)",
    )
    parser.add_argument("--context-window-tokens", type=int, default=None, help="Override resolved context window")
    args = parser.parse_args(argv)
    return ClawZeroConfig(
        model=args.model,
        agent_id=args.agent_id,
        tick_seconds=args.tick_seconds,
        base_dir=args.base_dir,
        compaction_threshold=args.compaction_threshold,
        max_tool_result_chars=args.max_tool_result_chars,
        context_window_tokens=args.context_window_tokens,
    )


async def _run(config: ClawZeroConfig) -> None:
    mailbox = Mailbox()
    human = StdioPeer(peer_id="human", agent_id=config.agent_id)
    peers = [human]

    agent = Agent.create(
        agent_id=config.agent_id,
        model=config.model,
        base_dir=config.base_dir,
        compaction_threshold=config.compaction_threshold,
        max_tool_result_chars=config.max_tool_result_chars,
        agents_md=_load_agents_md(),
        context_window=config.context_window_tokens,
    )

    print(
        f"claw-zero [{config.agent_id}] online — model {config.model}, "
        f"context window {agent.context_window:,} tokens. "
        "Type a message; Ctrl-D or Ctrl-C to exit.",
        flush=True,
    )

    background = [asyncio.create_task(human.inbound(mailbox))]
    if config.tick_seconds is not None:
        background.append(
            asyncio.create_task(tick_source(mailbox, config.tick_seconds, agent_id=config.agent_id))
        )

    loop_task = asyncio.create_task(run(mailbox, peers, agent))
    try:
        # The outer loop never returns on its own; we exit when stdin closes
        # (the inbound task finishes) or on interrupt.
        await background[0]
    finally:
        loop_task.cancel()
        for task in background[1:]:
            task.cancel()
        for task in [loop_task, *background[1:]]:
            try:
                await task
            except asyncio.CancelledError:
                pass


def main(argv: list[str] | None = None) -> None:
    config = _parse_args(argv)
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        print("\nclaw-zero: interrupted, shutting down.", flush=True)


if __name__ == "__main__":
    main()
