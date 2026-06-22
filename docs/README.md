# docs/

Analysis of the two codebases being merged into one minimal, **non-user-facing,
self-owned, long-running** agent loop with agent-to-agent communication.

| File | What it is |
|------|------------|
| [`ale-claw-summary.md`](./ale-claw-summary.md) | Summary of **ALE Claw** (this repo: `config.py`, `deployer.py`, `harness/`) — the minimal Python computer-use harness. Loop, prompts, tools, context, memory, subagents, model layer, with `file:line` references. |
| [`claude-code-summary.md`](./claude-code-summary.md) | Summary of **Claude Code** (`/Users/sshekkizhar/work/anthropic/claude-code/`) — the mature TS agent. Query loop, the section-assembled system prompt, ~40 tools, the **autonomous-work** prompt, and the multi-agent (SendMessage/Team/Cron) primitives. |
| [`comparison.html`](./comparison.html) | **Side-by-side HTML comparison** of prompts, tool descriptions, and harness mechanics — with verbatim quotes, a recommendations table (what to take from which), and a proposed merged-architecture blueprint. Open in a browser. |

## TL;DR of the recommendation

Use **ALE Claw** as the skeleton (lean headless loop, in-place compaction,
file-backed 2-layer memory + flush-before-compaction, gated "absence is the
signal" prompt, JSONL observability). Transplant from **Claude Code**: the
autonomous tick + `Sleep` pacing loop, the rich tool-description craft (examples
+ explicit anti-patterns + output rules) plus deferred-tool/ToolSearch scaling,
the safety/altitude prose (cyber-risk, reversibility, prompt-injection) as an
autonomous policy gate, and the **SendMessage / Team / Cron / coordinator**
agent-to-agent substrate. In every imported Claude Code prompt, "the user"
becomes "the requesting peer agent" or "the durable log".
