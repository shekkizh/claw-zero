# claw-zero

A **minimal, non-user-facing, self-owned, long-running agent loop — single agent
or a flat team of them.**

Its defining idea: **humans and agents are equal units/operators.** claw-zero
does not special-case "the user." It only ever:

1. **receives a message** addressed to it (from a human peer, another agent, or a
   self-tick), and
2. **sends a message** to some peer (a human or another agent).

**An activation ends when the agent delivers a message** — its plain-text reply *is* that message. The
outer loop then waits for the next message and goes again, forever.

Every participant — each agent and the human operator — has a **name** and is
addressed by it; the bus routes a message purely by its recipient name. So
running *many* agents is the same machinery applied N times: each agent owns its
own loop and inbox, and they all share one **message bus**. There is no lead and
no hierarchy — a flat peer mesh where coordination is emergent, via messages.

## Quickstart

```bash
# Install (openai is the only runtime LLM dependency).
uv pip install -e ".[dev]"     # or: pip install -e ".[dev]"
# Note: this repo's .venv is created by uv, which does NOT include pip — use
# `uv pip` (bare `pip` falls back to a system Python and fails requires-python).

# OpenAI credentials live in the environment, never config/argv:
export OPENAI_API_KEY=...

# Run the self-owned loop. You are just a peer over stdio.
python -m claw_zero
```

Then type a message and press Enter:

```
claw-zero [claw-zero] online — model gpt-5.5, context window 1,050,000 tokens. ...
What files are in the current directory? Then save a note that you checked.
[claw-zero] Files: alpha.txt, beta.md. I saved a note in .../memory/session-001.md.
```

The process never exits on its own; type another message for a follow-up.
Ctrl-D (stdin EOF) or Ctrl-C shuts it down gracefully (it finishes the in-flight
activation first).

### Useful flags

```bash
python -m claw_zero --model gpt-5.5                     # native OpenAI model id
python -m claw_zero --tick-seconds 60                   # self-tick every 60s (off by default)
python -m claw_zero --base-dir ./state                  # where memory + transcript live
python -m claw_zero --agents planner,coder,reviewer     # launch a team (flat peer mesh)
python -m claw_zero --no-spawn                          # forbid runtime spawn_agent
python -m claw_zero --help                              # all knobs
```

## A team of agents

Pass `--agents` to launch teammates alongside the primary agent. They all run
their own loops on one shared bus and address each other **by name**:

```bash
python -m claw_zero --agents coder,reviewer
# online: a team of three [claw-zero, coder, reviewer]. You are 'operator'.
# Address one agent with an @name or name: prefix; a bare line goes to the primary.
@coder refactor the parser in src/parse.py, then ask reviewer to check it
```

You (the human) are a named participant too — `operator` by default; rename
yourself with `--operator-id alex` so agents address you as `alex`.

What changes when there's more than one agent (and nothing otherwise — a lone
agent is byte-for-byte the original claw-zero):

- **Two team tools appear** ("absence is the signal" — they're only registered
  when the run is team-capable):
  - **`send_message(to, content)`** — reach a participant *other than* the one
    you're replying to, by name (a teammate, the operator, or `*` to broadcast
    to all teammates). It does **not** end your turn; your plain-text reply still
    goes to whoever last addressed you.
  - **`spawn_agent(id, model?, brief?)`** — bring a new teammate online at
    runtime as an equal peer, optionally with an opening brief. (Dropped under
    `--no-spawn`.)
- **A `# Team` section** is added to the system prompt, and Runtime context lists
  the participant names each agent can reach.
- **Messages between agents** are routed by the bus exactly like a reply to the
  operator — a teammate's message arrives as a fresh activation tagged
  `[message from <name>]`.

This **is** claw-zero's agent-to-agent system: agents coordinate by sending each
other messages over the bus. It is **in-process** — every agent is a coroutine in
one event loop, routed by recipient id. There is no network layer and no notion
of external/remote agents; the team is the set of agents you launch in this
process.

## The two-loop split

claw-zero deliberately separates the outer and inner loops into separate modules:

- **Outer loop** (`outer_loop.py`) — one agent's self-owned loop that never
  returns. It owns the durable cross-activation state (the running conversation,
  transcript, memory store, flush bookkeeping, tools) and the prompt assembly.
  Each turn it `receive`s the next message **from its own inbox**, appends it to
  the conversation, runs **one inner activation**, and `deliver`s the reply by
  routing `reply.recipient` through the **bus**. A team runs one of these loops
  per agent (see `team.py`); a single agent is the degenerate case.

- **Inner loop** (`inner_loop.py`) — one activation → one delivered message. It
  flushes memory if triggered, calls the model, runs any local Shell calls
  (plus `send_message`/`spawn_agent` on a team), compacts in place when over
  budget, and **returns the model's plain-text reply as the delivered
  `Message`**. That return value replaces `DONE`.

```
                ┌──────────────────── outer_loop.run (per agent, forever) ───────────────────┐
  bus ──msg──▶  │  inbox.receive → append → inner_loop.run(activation) → deliver(reply)→ bus  │ ──msg──▶ bus
                └──────────────────────────────┬─────────────────────────────────────────────┘
                                               │  one activation:
                                  flush? → llm.call → [shell | send_message | spawn_agent]* → compact? → reply Message
```

## Named participants, equal operators

Everything moves through one channel — the **bus** (`messaging/bus.py`), which
routes a `Message` to either an agent's inbox (a `Mailbox`, `messaging/mailbox.py`,
an `asyncio.Queue`) or an external **peer**'s `outbound()`. Every participant has
a **name**: agents by their id, the human operator by `operator_id`. A peer
(`messaging/peer.py`) bridges the operator's stdio channel into the bus as
`StdioPeer` — it exists so the human can be addressed by name like any agent, not
as a hook for remote agents (there are none). Nothing in the loop branches on
*who* the sender is. The only allowed message branch is `kind` (`"tick"` vs
`"message"`). A message is routed purely by its `recipient` **name**, so "send to
a teammate" and "reply to the operator" are the same operation.

A **tick** is "you're awake, what now?". If there's nothing useful to do, the
agent **sleeps** — it replies with empty text, and the loop delivers nothing and
waits for the next message.

## Tool surface

claw-zero exposes OpenAI's native local `shell` Responses tool, backed by the
client-side subprocess executor in `tools/bash.py`. It is also the file tool:

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
read/write/edit/grep/glob tools and no permission gate.

Agents also receive OpenAI's hosted `web_search` Responses tool for up-to-date
public information. It is not dispatched locally; OpenAI runs the search inside
the Responses API call and returns text with URL citation annotations.

## Durable memory

Memory (`memory/store.py`) is file-backed and agent-scoped:

```
claw_zero_state/<agent_id>/
├── AGENT_MEMORY.md          # curated, full-overwrite knowledge
└── memory/
    └── session-NNN.md       # append-only session log (scratchpad)
```

The agent reads and writes these **via shell** (their absolute paths are surfaced
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
├── __main__.py            # entry point — builds the Team, runs it until stdin EOF
├── config.py              # ClawZeroConfig (roster + spawn knobs; no LLM effort knob)
├── llm.py                 # single OpenAI Responses call + context-window fallback
├── prompt.py              # gated "absence is the signal" builder (+ # Team section)
├── AGENTS.md              # the persistent operating doc (peer-among-peers ethos)
├── team.py                # Team orchestrator — owns the bus, roster, loops, spawn
├── outer_loop.py          # one agent's self-owned loop + Agent (durable state)
├── inner_loop.py          # one activation → one delivered Message
├── messaging/             # bus.py (MessageBus routing), mailbox.py (Mailbox/Message), peer.py (Peer/StdioPeer/tick)
├── tools/                 # bash.py (local Shell executor), send_message.py, spawn_agent.py, registry.py
├── context/               # token_estimation.py, compaction.py, transcript.py
├── memory/                # store.py (MemoryStore), flush.py (pre-compaction flush)
├── tests/                 # 68 tests (pytest)
└── docs/                  # PORTING.md (KEEP/PORT/DROP map), summaries, comparison, TODO
```
