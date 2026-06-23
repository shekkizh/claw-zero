# claw-zero

A **minimal, non-user-facing, self-owned, long-running agent loop.**

Its defining idea: **humans and agents are equal units/operators.** claw-zero
does not special-case "the user." It only ever:

1. **receives a message** addressed to it (from a human peer, another agent, or a
   self-tick), and
2. **sends a message** to some peer (a human or another agent).

**An activation ends when the agent delivers a message** — its plain-text reply *is* that message. The
outer loop then waits for the next message and goes again, forever.

## Quickstart

```bash
# Install (litellm is the only runtime dependency).
uv pip install -e ".[dev]"     # or: pip install -e ".[dev]"
# Note: this repo's .venv is created by uv, which does NOT include pip — use
# `uv pip` (bare `pip` falls back to a system Python and fails requires-python).

# A provider key in the environment (litellm reads it — never config/argv):
export OPENAI_API_KEY=...       # or ANTHROPIC_API_KEY / OPENROUTER_API_KEY / ...

# Run the self-owned loop. You are just a peer over stdio.
python -m claw_zero
```

Then type a message and press Enter:

```
claw-zero [claw-zero] online — model openai/gpt-5.5, context window 1,050,000 tokens. ...
What files are in the current directory? Then save a note that you checked.
[claw-zero] Files: alpha.txt, beta.md. I saved a note in .../memory/session-001.md.
```

The process never exits on its own; type another message for a follow-up.
Ctrl-D (stdin EOF) or Ctrl-C shuts it down gracefully (it finishes the in-flight
activation first).

### Useful flags

```bash
python -m claw_zero --model anthropic/claude-opus-4-8   # any LiteLLM model id
python -m claw_zero --tick-seconds 60                   # self-tick every 60s (off by default)
python -m claw_zero --base-dir ./state                  # where memory + transcript live
python -m claw_zero --help                              # all knobs
```

## The two-loop split

claw-zero deliberately separates the outer and inner loops into separate modules:

- **Outer loop** (`outer_loop.py`) — the self-owned loop that never returns. It
  owns the durable cross-activation state (the running conversation, transcript,
  memory store, flush bookkeeping, tools) and the prompt assembly. Each turn it
  `receive`s the next message, appends it to the conversation, runs **one inner
  activation**, and `deliver`s the reply by routing `reply.recipient` to that
  peer's `outbound()`.

- **Inner loop** (`inner_loop.py`) — one activation → one delivered message. It
  flushes memory if triggered, calls the model, runs any `bash` tool calls,
  compacts in place when over budget, and **returns the model's plain-text reply
  as the delivered `Message`**. That return value replaces `DONE`.

```
                ┌─────────────────────── outer_loop.run (forever) ───────────────────────┐
  peers ──msg──▶│  mailbox.receive → append → inner_loop.run(activation) → deliver(reply) │──msg──▶ peers
                └──────────────────────────────┬──────────────────────────────────────────┘
                                               │  one activation:
                                  flush? → llm.call → [bash]* → compact? → reply Message
```

## Humans and agents are equal peers

Everything moves through one channel — the **mailbox** (`messaging/mailbox.py`,
an `asyncio.Queue`). A **peer** (`messaging/peer.py`) bridges some external
channel to the mailbox; the human is just `StdioPeer`. Nothing in the loop
branches on `sender == "human"`. The only allowed message branch is `kind`
(`"tick"` vs `"message"`). A reply is routed purely by `recipient` id, so a
human peer and a (future) agent peer are interchangeable.

A **tick** is "you're awake, what now?". If there's nothing useful to do, the
agent **sleeps** — it replies with empty text, and the loop delivers nothing and
waits for the next message.

## The single tool: `bash`

claw-zero has exactly one tool — `bash` (`tools/bash.py`), a **client-side,
local subprocess**. It is also the file tool:

| Need | Use |
|---|---|
| read a file | `cat path` / `sed -n '1,40p' path` |
| search contents | `grep -rn "pat" .` / `rg "pat"` |
| find files | `find . -name '*.py'` |
| edit in place | `sed -i ...` / `python -c ...` |
| write a file | redirection / `python - <<'PY' ...` |

The working directory **persists** between calls; shell state (env vars,
functions) does **not** — each call is a fresh `/bin/sh`. Timeouts kill the whole
process group, so children aren't orphaned. There are no dedicated
read/write/edit/grep/glob tools, no web search, and no permission gate.

## Durable memory

Memory (`memory/store.py`) is file-backed and agent-scoped:

```
claw_zero_state/<agent_id>/
├── AGENT_MEMORY.md          # curated, full-overwrite knowledge
└── memory/
    └── session-NNN.md       # append-only session log (scratchpad)
```

The agent reads and writes these **via bash** (their absolute paths are surfaced
in the prompt's Runtime context). Before context is compacted, a
**flush-before-compaction** turn (`memory/flush.py`) gives the model one chance
to persist durable memory via a `memory_write` call routed to the store — so
durable memory survives context loss on long runs.

## Context & compaction

Long runs compact **in place** (`context/compaction.py`): recent turns are
preserved, older history is LLM-summarized into a checkpoint, and
tool_call/tool_result pairing is repaired so the conversation stays valid. An
append-only JSONL transcript (`context/transcript.py`) records every turn.

## Layout

The package modules live at the repo root (the importable package name is
`claw_zero`; `pyproject.toml` maps it onto this directory):

```text
claw-zero/                 # repo root == the claw_zero package
├── pyproject.toml         # packaging — maps the claw_zero package onto the root
├── __main__.py            # entry point — wires everything, runs the outer loop
├── config.py              # ClawZeroConfig (no effort knob — effort is always max)
├── llm.py                 # single litellm call + thinking/effort + model resolve + cache policy
├── prompt.py              # gated "absence is the signal" system-prompt builder
├── AGENTS.md              # the persistent operating doc (peer-among-peers ethos)
├── outer_loop.py          # self-owned loop + Agent (durable state)
├── inner_loop.py          # one activation → one delivered Message
├── messaging/             # mailbox.py (Mailbox/Message), peer.py (Peer/StdioPeer/tick)
├── tools/                 # bash.py (the one tool), registry.py (build_tools split)
├── context/               # token_estimation.py, compaction.py, transcript.py
├── memory/                # store.py (MemoryStore), flush.py (pre-compaction flush)
├── tests/                 # 50 tests (pytest)
└── docs/                  # PORTING.md (KEEP/PORT/DROP map), summaries, comparison, TODO
```

## Deferred by design

These are intentionally **absent** (TODO markers, not built):

- **web search**, computer use / GUI, images, vision
- **subagent delegation**, teams
- **A2A (agent-to-agent) network transport** — the mailbox is in-memory now, but
  behind an interface so a real transport drops in later
- **cron / scheduling**
- a **policy / permission gate**
- the `DONE` signal (replaced by "deliver a message")

See `docs/comparison.html` §09 for the merged-architecture blueprint this milestone
implements.
