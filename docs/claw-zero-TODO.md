# claw-zero — Implementation TODO

> **You are a long-running implementation agent.** This file is your complete brief.
> Work the phases in order, top to bottom. Each task has an **acceptance check** —
> do not mark a task done until its check passes. Commit after each phase. Keep a
> running log in `claw_zero/memory/` as you go (you are building the thing that
> needs memory; eat your own dog food once Phase 5 lands).
>
> **Read these first** (they are the design rationale; do not re-derive it):
> - `docs/ale-claw-summary.md` — the Python harness we are stripping down (lives in `harness/`).
> - `docs/claude-code-summary.md` — the agent we are borrowing prompt/tool craft from.
> - `docs/comparison.html` — side-by-side + the merged-architecture blueprint (§09).

---

## 0. What claw-zero is (north star — re-read if a decision feels ambiguous)

claw-zero is a **minimal, non-user-facing, self-owned, long-running agent loop**.
Its defining idea: **humans and agents are equal units/operators.** The agent does
not special-case "the user." It only ever:

1. receives a **message** addressed to it (from a human peer, another agent, or a self-tick), and
2. sends a **message** to some peer (a human or another agent).

There is no "user-facing" mode and no "DONE" terminal state. **An activation ends
when the agent delivers a message** (a text reply) to whoever it is talking to.
The outer loop then waits for the next message and goes again, forever.

**Hard scope decisions (do not revisit):**
- **Language:** Python 3.12+. Reuse/strip the existing `harness/` code where useful.
- **LLM client:** `litellm` (already a dependency). Keep LiteLLM model-string format so
  Anthropic / OpenAI / Amazon Bedrock/ Google Vertex all work. **Do NOT** add any additional SDK.
- **Drop completely:** `cua-agent`, `cua-computer`, `cua-core`, all computer-use / GUI /
  screenshot / VM code, the `canonical/` and `model/unified_loop.py` SDK-bridge layers,
  image handling, and the `subagent/` delegation tools. Remove these deps from `pyproject.toml`.
- **Tools (minimal):** exactly one —
  - `bash` (client-side, local subprocess) — the *only* tool. It covers file read/write/search/edit
    via the shell (`cat`/`grep`/`find`/`sed`/`ls`/`python -c`). No dedicated read/write/edit/grep/glob
    tools, no web search, no policy/permission gate.
- **Messaging transport:** in-memory mailbox now; **human is just a peer over stdio.**
  Keep it behind an interface so a real agent-to-agent (A2A) transport drops in later.
  **A2A substrate is explicitly deferred — do not build it.**
- **Outer loop vs inner loop are separate modules** (the user asked for this split).

**The harness is the source of truth — do not re-spell its APIs from this doc or from memory.**
`harness/` already implements the LLM call, thinking/effort mapping, model resolution, context
estimation, compaction, transcript, and memory. This TODO names *which* harness pieces to keep
and *how* to wire them — it deliberately does **not** restate their function signatures, return
shapes, constants, or provider quirks. When a phase says "port X," open the referenced
`harness/` file and reuse what is there; treat any code shape sketched below as illustrative
pseudocode, not a spec to match. If this doc and the harness disagree, the harness wins.

**Ground-truth facts (settled — do not "correct" these from memory):**
- Default model: `openai/gpt-5.5` (LiteLLM format).
- Thinking/effort is **always max** wherever the harness applies it. Use the harness's existing
  thinking layer (`harness/model/thinking.py`) — pass its highest effort level on every call site
  (main loop, compaction, memory flush). Do not invent a new effort knob; do not lower it per-site.
- Prompt caching is a **prefix match**: keep the system prompt byte-stable; never interpolate
  a timestamp/uuid into it. Put volatile content last. (The harness already handles cache markers —
  reuse `harness/model/cache_policy.py` rather than re-implementing.)

**Out of scope for this milestone (write `log()`/TODO markers, don't build):**
computer use, GUI, images, web search, subagent delegation, A2A network transport, teams, cron,
policy/permission gate, the `DONE` signal.

---

## 1. Phase 0 — Scaffolding

- [ ] **0.1 Create the package skeleton** at repo root:
  ```
  claw_zero/
    __init__.py
    __main__.py            # entrypoint (Phase 9)
    config.py              # trimmed config (Phase 9)
    outer_loop.py          # Phase 8
    inner_loop.py          # Phase 7
    llm.py                 # Phase 2
    prompt.py              # Phase 4
    AGENTS.md              # Phase 4 (the persistent operating doc)
    messaging/
      __init__.py
      mailbox.py           # Phase 1
      peer.py              # Phase 1 (Peer + StdioPeer)
    tools/
      __init__.py
      registry.py          # Phase 3
      bash.py              # Phase 3
    context/
      __init__.py
      compaction.py        # Phase 6 (ported from harness/context/)
      token_estimation.py  # Phase 6 (ported)
      transcript.py        # Phase 6 (ported, trimmed)
    memory/
      __init__.py
      store.py             # Phase 5 (ported from harness/memory/memory.py)
      flush.py             # Phase 5 (ported from harness/memory/memory_flush*.py)
  ```
  **Acceptance:** `python -c "import claw_zero"` succeeds; `tree claw_zero` matches above.

- [ ] **0.2 Trim `pyproject.toml`** — remove `cua-agent`, `cua-computer`, `cua-core`,
  `Pillow`. Keep `litellm>=1.80`. Add `pytest` (dev). Rename project to `claw-zero`.
  **Acceptance:** `pip install -e .` (or `uv sync`) succeeds with no cua/* present;
  `pip list | grep -i cua` is empty.

- [ ] **0.3 Decide reuse vs rewrite.** For each `harness/` module, the action is in
  `docs/ale-claw-summary.md` §"Per-module" table in `docs/comparison.html`. Port
  `context/` and `memory/` (strip image-token logic); rewrite the loop; delete the rest.
  **Acceptance:** a short note `claw_zero/PORTING.md` listing, per source file, KEEP/PORT/DROP.

---

## 2. Phase 1 — Messaging (the equal-operator substrate)

Port the **shape** of Claude Code's mailbox (`/Users/sshekkizhar/work/anthropic/claude-code/utils/mailbox.ts`)
to Python on an `asyncio.Queue` (ALE Claw already uses `asyncio.Queue` for subagent results —
same idea). This is the *only* channel; the human and any future agent both speak through it.

- [ ] **1.1 `messaging/mailbox.py`** — define:
  ```python
  @dataclass
  class Message:
      id: str
      sender: str        # peer id, e.g. "human" or an agent name. NOT special-cased.
      recipient: str     # this agent's id, or a peer id for outgoing
      content: str
      kind: str = "message"   # "message" | "tick"  (room to grow; treat uniformly)
      ts: str = ""       # ISO8601; pass in, do not call datetime.now() in hot paths you cache

  class Mailbox:
      async def send(self, msg: Message) -> None      # enqueue
      async def receive(self) -> Message              # await next (blocks)
      def poll(self) -> Message | None                # non-blocking
  ```
  Back it with `asyncio.Queue`. **Acceptance:** unit test — send 3, receive 3 in FIFO order;
  `poll()` returns `None` when empty.

- [ ] **1.2 `messaging/peer.py`** — a transport interface and the stdio implementation:
  ```python
  class Peer(Protocol):           # a unit/operator the agent can talk to
      id: str
      async def inbound(self, mailbox: Mailbox) -> None   # read external -> mailbox.send(...)
      async def outbound(self, msg: Message) -> None      # deliver agent's message externally

  class StdioPeer:                # the human, as just another peer
      id = "human"
      # inbound: read lines from stdin, wrap each as Message(sender="human", kind="message")
      # outbound: print msg.content to stdout (prefixed with the agent id for clarity)
  ```
  A self-tick source is just a coroutine that periodically does `mailbox.send(Message(kind="tick", sender="self"))`.
  **Acceptance:** a manual `python -m claw_zero.messaging.peer` demo where typing a line on
  stdin shows up as a received `Message`, and `outbound()` prints to stdout.

> **Design check:** nothing in the loop should branch on `sender == "human"`. A human and an
> agent are interchangeable peers. The only kind-branch allowed is `tick` vs `message`.

---

## 3. Phase 2 — LLM core (single model call via litellm)

> First read how the harness already calls litellm and maps effort: `harness/model/thinking.py`,
> `harness/model/model_config.py`, and the `predict_step`/completion call sites in
> `harness/agent_loop.py`. Reuse that machinery; `llm.py` is a thin wrapper, not a re-implementation.

- [ ] **2.1 `llm.py`** — expose one function that does a single tool-calling model call and returns
  a small normalized result (text, tool_calls, finish_reason, usage). Drive it through litellm in
  the same way the harness does, reusing the harness's thinking/effort and model-config layers (so
  effort is always max and provider quirks are already handled). Do not hardcode the litellm
  request/response shape here — take it from the harness code you're reusing.
  **Acceptance:** with `OPENAI_API_KEY` set, a call to `openai/gpt-5.5` with a trivial prompt and
  no tools returns non-empty text and a normal (non-tool) finish; effort passed is the harness's max.

---

## 4. Phase 3 — Tools (exactly one)

> Follow the existing tool pattern: `harness/tools/tools_shell.py` already implements an `exec`
> tool (description, schema, validation, truncation). claw-zero's `bash` is that tool re-pointed
> from the VM to a **local** `subprocess` — reuse its shape and the harness truncation helper
> (`harness/context/context.py`) rather than writing new boilerplate.

- [ ] **3.1 `tools/bash.py`** — a **client-side** `bash(command, timeout?)` tool that runs the
  command locally and returns stdout/stderr/exit_code, truncated via the harness helper. Adapt
  `tools_shell.py`'s `exec` (drop the VM/MCP transport; run locally in the agent's working dir).
  **Write the description with Claude Code richness** (`docs/claude-code-summary.md` §4 Bash):
  state that bash *is* the file tool here (read with `cat`/`sed -n`, search with `grep -rn`/`rg`,
  find with `find`, edit with `sed`/`python -c`), note "cwd persists; shell state does not", and
  document the timeout.
  **Acceptance:** `{"command":"echo hi && pwd"}` returns exit_code 0 with `hi` in stdout; a
  `sleep 999` with `timeout:1` returns a timeout error, not a hang.

- [ ] **3.2 `tools/registry.py`** — assemble the single-tool surface the loop needs: a list of
  tool specs (just `bash`'s) and a `name -> handler` map (just `bash`). Mirror
  `harness/tools/tools.py`'s `build_tools` / `get_tool_summaries` split so the prompt builder can
  read tool summaries the same way. **Acceptance:** returns exactly one spec and one handler;
  prompt builder (Phase 4) consumes the summaries without special-casing.

---

## 5. Phase 4 — Prompt builder

Port the **gated "absence is the signal"** assembly from `harness/prompt.py` (don't reinvent),
but rewrite the prose for a non-user-facing peer agent, importing the high-value Claude Code
sections (quoted verbatim in `docs/claude-code-summary.md`).

- [ ] **4.1 `prompt.py`** — sections, in order, each gated on relevance:
  1. **Identity** — rewrite. e.g.: *"You are claw-zero, a self-owned autonomous agent. You
     operate continuously. You communicate with peers — humans and other agents alike — by
     exchanging messages; you treat every peer as an equal operator. You have no user
     interface."*
  2. **Operating loop** — explain: you receive a message, you may use tools, and **you finish a
     turn by sending a reply message to the peer who last addressed you** (your plain-text
     response is that message). There is no "done" marker.
  3. **Tools** — the gated tool list (`bash`) with their descriptions.
  4. **Memory** — port ALE Claw's Memory Recall prose (session log + curated TASK/AGENT memory;
     "write it down, no mental notes"). The agent reads/writes memory **via bash** (no memory tool).
  5. **Behavior & altitude** — import from Claude Code (`docs/claude-code-summary.md` §2):
     *# Doing tasks* (read before you change; minimal complexity; only validate at boundaries),
     *faithful reporting* (never claim success you didn't verify; state failures with output),
     and *executing actions with care* (reversibility / blast radius) — **repointed from "the
     user" to "the requesting peer / the durable log."**
  6. **Autonomy & pacing** — import Claude Code's *# Autonomous work* (`docs/claude-code-summary.md`
     §3): bias to action, and **"if you have nothing useful to do on a tick, sleep"** (here
     "sleep" = return without sending, so the outer loop waits for the next message).
  - Keep the static prefix byte-stable for caching; inject date/peer/runtime context **after** a
    boundary marker (mirror ALE Claw's `# Project Context`).
  **Acceptance:** `build_prompt(tools=[...])` with no tools omits the Tools/Memory sections
  entirely (no "X is disabled" text appears); with tools present they appear. Snapshot test the
  assembled string.

- [ ] **4.2 `claw_zero/AGENTS.md`** — port `harness/AGENTS.md`, keep the "this environment is
  home" + two-layer memory ethos, **delete** the `DONE`/screenshot lines, and add a short
  "you are a peer among peers; reply by sending a message" note. Injected as a context file by 4.1.
  **Acceptance:** file exists and is referenced by `build_prompt`.

---

## 6. Phase 5 — Memory (durable, file-backed)

Port `harness/memory/` — this is the keeper feature.

- [ ] **5.1 `memory/store.py`** — port `MemoryStore`: layout
  `claw_zero_state/<agent_id>/{AGENT_MEMORY.md, memory/session-NNN.md}`. Methods:
  `init_session()`, `append_session(text)`, `write_curated(text)` (full overwrite),
  `read_curated()`, `read_file(path, start, end)`. Keep the `Path.is_relative_to` traversal guard.
  **Acceptance:** unit test creates a session, appends two entries, overwrites curated memory,
  reads it back.

- [ ] **5.2 `memory/flush.py`** — port the **flush-before-compaction** turn
  (`harness/memory/memory_flush*.py`): a pre-compaction LLM call (via `llm.call`) that writes
  durable memory before old context is summarized away. Keep the token/byte triggers and the
  dedup guard. The flush "tool" is just `memory_write(content, target)` routed to the store.
  **Acceptance:** unit test — force the trigger, assert a session/curated file gains content
  (mock `llm.call` to return one `memory_write` call).

---

## 7. Phase 6 — Context & compaction

Port `harness/context/` (strip all image-token logic).

- [ ] **6.1 `context/token_estimation.py`** — port the `len(json)/4 * 1.2` estimator; **remove**
  the `FIXED_IMAGE_TOKENS` path. **Acceptance:** returns a positive int for a sample message list.
- [ ] **6.2 `context/compaction.py`** — port `compact_messages`: preserve last N turns, budget
  kept history at `context_window * 0.4`, LLM-summarize older chunks (via `llm.call`), repair
  tool_call/tool_result pairing. **Acceptance:** unit test — feed an over-budget message list,
  assert output fits budget and summary text is present, and tool_call/result pairs are intact.
- [ ] **6.3 `context/transcript.py`** — port the append-only JSONL transcript (session header,
  message entries with usage, compaction entries). Drop image entries. **Acceptance:** running
  an activation writes a readable `transcript.jsonl`.

---

## 8. Phase 7 — Inner loop (one activation → one delivered message)

- [ ] **7.1 `inner_loop.py`** — `async def run(activation_ctx) -> Message`:
  ```
  loop:
    flush_memory_if_triggered()                 # Phase 5
    result = llm.call(system, messages, tools)  # Phase 2 — effort fixed at max inside llm.call
    append assistant turn to messages + transcript
    if result.tool_calls:                       # bash is the only client-side tool
        for call in result.tool_calls:
            out = handlers[call.name](args)      # bash
            append tool result message (role="tool", tool_call_id=...)
        if over_budget(): compact_in_place()     # Phase 6
        continue
    else:                                        # no tool call → plain text reply
        return Message(sender=self_id, recipient=incoming.sender, content=result.text)
  ```
  **The return value IS the delivered message — this replaces "DONE".** 
  **Acceptance:** given an incoming message "run `echo hello` and tell me the output", the
  inner loop performs one bash tool call and returns a `Message` whose content mentions `hello`.

---

## 9. Phase 8 — Outer loop (self-owned, never returns)

- [ ] **8.1 `outer_loop.py`** — `async def run(mailbox, peers, agent)`:
  ```
  while True:
      msg = await mailbox.receive()             # human peer | agent peer | self-tick
      if msg.kind == "tick" and nothing_to_do(): continue   # "sleep": just wait for next
      append msg to conversation as an incoming peer message
      reply = await inner_loop.run(...)         # Phase 7 — ends on a delivered message
      await deliver(reply, peers)               # route reply.recipient -> that peer.outbound()
      # loop forever; no exit condition
  ```
  - `deliver()` looks up the recipient peer and calls its `outbound()`. For now the only peer is
    the human (stdio), so replies print to stdout.
  - Optional: a background tick coroutine that enqueues `kind="tick"` messages on an interval
    (the Claude Code pacing pattern). Keep it off by default behind a config flag.
  **Acceptance:** end-to-end — start the process, type a message on stdin, see the agent's reply
  on stdout, type another, see another reply; the process never exits on its own.

---

## 10. Phase 9 — Entry point & config

- [ ] **9.1 `config.py`** — start from the existing `config.py` and **delete** every
  cua/GUI/transport/delegation/web/thinking-level knob, keeping only what claw-zero uses:
  `model` (default `openai/gpt-5.5`), `context_window_tokens`, `compaction_threshold` (0.8),
  `max_tool_result_chars` (16000), `tick_seconds` (None=off), `agent_id` ("claw-zero"). **No
  effort knob** — effort is always max via the harness thinking layer (see §0). **Acceptance:**
  config validation passes and importing it pulls in no `cua-*` modules.
- [ ] **9.2 `__main__.py`** — wire it up: build config → mailbox → StdioPeer (+ optional tick) →
  memory store → prompt → tools → run `outer_loop`. Read API keys from env only (never config).
  **Acceptance:** `python -m claw_zero` starts an interactive stdio session against the real model.

---

## 11. Phase 10 — Verify & document

- [ ] **10.1 Smoke test (must pass before calling the milestone done):**
  1. `OPENAI_API_KEY=... python -m claw_zero`
  2. Type: `What files are in the current directory? Then save a note that you checked.`
  3. Expect: a `bash` (`ls`) tool call, a memory write via bash to `session-NNN.md`, and a plain
     text reply listing files — delivered to stdout. Process stays alive for a follow-up.
  **Acceptance:** all three observed; `transcript.jsonl` and a `session-NNN.md` exist afterward.
- [ ] **10.2 `claw_zero/README.md`** — quickstart, the outer/inner-loop split, the
  "humans and agents are equal peers" model, the single-tool (`bash`) surface, and the
  **deferred** list (web search, A2A transport, teams, cron, more peers). Link back to
  `docs/comparison.html` §09 blueprint.
  **Acceptance:** a new reader can run it from the README alone.
- [ ] **10.3 Tests green:** `pytest claw_zero/` passes (unit tests from 1.1, 5.x, 6.x, plus the
  prompt snapshot from 4.1).

---

## 12. Definition of done (the whole milestone)

- `python -m claw_zero` runs a self-owned loop that never exits on its own.
- A human peer over stdio can converse with it; the agent treats the human as one peer among
  (future) many — no `sender=="human"` special-casing anywhere.
- The agent uses exactly `bash` (client-side).
- An activation ends by **delivering a message**, never by emitting "DONE".
- Durable memory (session log + curated) and flush-before-compaction work; long runs compact
  in place without losing tool pairing.
- No `cua-*` dependency, no computer-use/GUI/image/subagent code remains.
- A2A transport, teams, cron, and any policy gate are **absent by design** (TODO markers only).

> **When done, append a one-line memory** to `claw_zero/memory/session-001.md` recording what
> shipped and any surprises — then this agent has used the very loop it built.
