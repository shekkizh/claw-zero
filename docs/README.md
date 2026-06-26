# docs/

Analysis of the two codebases that were merged into one minimal, **non-user-facing,
self-owned, long-running** agent loop with agent-to-agent communication — plus the
implementation brief for the result, **claw-zero**.

> **Status:** claw-zero is **built** — all 11 phases shipped, 68 tests green, and a
> live smoke test passed against `gpt-5.5` (see the package `README.md` and
> `memory/session-001.md`). A later `reorganize folders` commit flattened the
> package to the repo root and removed the vendored harness; §13 restored
> packaging/imports. The current open design work is the reloadable
> self-modifying harness tracked in [`claw-zero-TODO.md`](./claw-zero-TODO.md) §15.

| File | What it is |
|------|------------|
| [`claw-zero-TODO.md`](./claw-zero-TODO.md) | **Implementation TODO / status.** The phase-by-phase brief, annotated with what shipped, the post-reorg follow-ups (§13), team milestone (§14), and the new reloadable self-modifying harness TODO (§15). Start here. |
| [`PORTING.md`](./PORTING.md) | **Per-source KEEP / PORT / DROP map** from the ALE Claw harness into claw-zero. Reflects the post-reorg (repo-root) destination paths. |
| [`ale-claw-summary.md`](./ale-claw-summary.md) | Summary of **ALE Claw** — the minimal Python computer-use harness that was stripped down. Loop, prompts, tools, context, memory, subagents, model layer, with `file:line` references. The harness is **no longer in this repo**; its source lives at `…/benchmarks/agents-last-exam/ale_run/agents/ale_claw/`. |
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
