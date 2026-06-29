# claw-zero — Implementation TODO / Status

> **Status: built, reorganized, and re-packaged — runnable again.** All 11 phases
> (0–10) were implemented, committed, and verified — 68 tests green and a live
> smoke test against `gpt-5.5` (see `memory/session-001.md`). A subsequent
> commit (`fca034b "reorganize folders"`) **flattened the package from
> `claw_zero/` into the repo root** and removed the in-repo `harness/` and
> `legacy/` snapshots, `pyproject.toml`, and the per-package `PORTING.md`/`README.md`.
> That reorg left the package un-runnable as `python -m claw_zero`; **§13
> Post-reorg follow-ups** restored packaging (a `pyproject.toml` that maps the
> `claw_zero` package onto the repo root). The next open design work is **§15:
> reloadable self-modifying harness** — making source edits take effect through a
> process-level reload boundary while preserving durable agent state.

> **Read these first** (design rationale — do not re-derive it):
> - `docs/ale-claw-summary.md` — the Python harness that was stripped down. The
>   harness is **no longer in this repo**; its source now lives at
>   `…/benchmarks/agents-last-exam/ale_run/agents/ale_claw/harness/`.
> - `docs/claude-code-summary.md` — the agent whose prompt/tool craft was borrowed.
> - `docs/comparison.html` — side-by-side + the merged-architecture blueprint (§09).
> - `docs/PORTING.md` — per-source KEEP/PORT/DROP map (harness → claw-zero).

---

## Phase status at a glance

| Phase | What | Status |
|---|---|---|
| 0 | Scaffolding (skeleton, deps, port plan) | ✅ shipped (layout later flattened — see §13) |
| 1 | Messaging substrate (`mailbox`, `peer`) | ✅ shipped |
| 2 | LLM core (`llm.py` via OpenAI SDK) | ✅ shipped |
| 3 | Tools — OpenAI local `shell` + hosted OpenAI `web_search` | ✅ shipped |
| 4 | Prompt builder + `AGENTS.md` | ✅ shipped |
| 5 | Durable memory (`store`, `flush`) | ✅ shipped |
| 6 | Context & compaction (chat-shape) | ✅ shipped |
| 7 | Inner loop (one activation → one message) | ✅ shipped |
| 8 | Outer loop (self-owned, never returns) | ✅ shipped |
| 9 | Entry point & config | ✅ shipped |
| 10 | Verify & document | ✅ shipped (68 tests green, live smoke test) |
| 13 | Post-reorg follow-ups | ✅ done — `pyproject.toml` restored; runnable, 68 tests pass |
| 14 | **Team of agents** (flat peer mesh) | ✅ shipped — bus + send_message + spawn_agent; 68 tests pass + live 2-agent smoke test |
| 15 | **Reloadable self-modifying harness** | ✅ MVP shipped — supervised restart/reload boundary, runtime state resume, source identity logging, and reload smoke tests |

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

**Hard scope decisions (settled — do not revisit):**
- **Language:** Python 3.12+ (the build runs on 3.13). Self-contained — claw-zero
  does **not** import `harness.*` (the harness `__init__.py` eagerly pulls the cua
  SDK). Everything kept was **ported** into the package, not imported.
- **LLM client:** OpenAI SDK only. Use native OpenAI model ids; the old
  `openai/<id>` prefix is accepted as a compatibility shim, but other provider
  prefixes are rejected.
- **Dropped completely:** `cua-agent`, `cua-computer`, `cua-core`, `Pillow`, all
  computer-use / GUI / screenshot / VM code, the `canonical/` and
  `model/unified_loop.py` SDK-bridge layers, image handling, and `subagent/`
  delegation.
- **Tools (minimal):** OpenAI local `shell` (client-side subprocess) plus OpenAI's hosted
  `web_search` Responses tool. `shell` is also the file tool
  (`cat`/`grep`/`find`/`sed`/`python -c`). No dedicated read/write/edit/grep/glob
  tools and no policy/permission gate.
- **Messaging transport:** in-memory bus + per-agent mailboxes; **the human is a
  named participant over stdio**, addressed by name exactly like an agent. This
  in-process message-passing **is** the agent-to-agent (A2A) system — it is the
  point of the project, not a placeholder. External/remote A2A network transport
  is **not a goal**. A local supervisor/worker process boundary for harness
  reload is a separate lifecycle concern tracked in §15, not a network transport.
- **Outer loop vs inner loop are separate modules.**

**Ground-truth facts (settled — do not "correct" these):**
- Default model: `gpt-5.5` (native OpenAI model id).
- Reasoning effort is fixed in `llm.py` as the Responses API block
  `reasoning={"effort": "xhigh"}` on every call site (main loop, compaction,
  memory flush). There is **no per-site effort knob**.
- Prompt caching is a **prefix match**: the system prompt prefix is byte-stable;
  volatile content sits below `CACHE_BOUNDARY` (`<!-- CLAW_ZERO_CACHE_BOUNDARY -->`).
  `llm.py` consumes the marker before sending the prompt to OpenAI.

**Out of scope for this milestone (TODO markers only — not built):**
computer use, GUI, images, subagent delegation, A2A network transport,
teams, cron, policy/permission gate, the `DONE` signal.

---

## Current layout (post-reorg — the package is now the repo root)

After `fca034b`, the package files live **directly at the repo root** (the dir is
`claw-zero/`); there is no longer a nested `claw_zero/` directory, and `harness/`
and `legacy/` are gone.

```
claw-zero/                    # repo root == the package
  __init__.py                 # version + package docstring (lazy submodule imports)
  __main__.py                 # entry point (Phase 9)
  config.py                   # ClawZeroConfig (Phase 9)
  llm.py                      # single OpenAI Responses call + context fallback (Phase 2)
  prompt.py                   # gated "absence is the signal" builder (Phase 4)
  AGENTS.md                   # persistent operating doc (Phase 4)
  outer_loop.py               # self-owned loop + Agent (Phase 8)
  inner_loop.py               # one activation → one delivered Message (Phase 7)
  messaging/
    mailbox.py                # Mailbox + Message (Phase 1)
    peer.py                   # Peer + StdioPeer + tick_source (Phase 1)
  tools/
    registry.py               # build_tools / get_tool_summaries (Phase 3)
    bash.py                   # local Shell executor (Phase 3)
  context/
    compaction.py             # chat-shape compaction (Phase 6)
    token_estimation.py       # chars/4 estimator, no image path (Phase 6)
    transcript.py             # append-only JSONL (Phase 6)
  memory/
    store.py                  # MemoryStore (Phase 5)
    flush.py                  # flush-before-compaction (Phase 5)
    session-001.md            # the build's own dog-food log
  tests/                      # 68 tests (still import the `claw_zero` package name)
  README.md                   # quickstart + architecture (the canonical README)
  docs/                       # this analysis set (TODO, PORTING, summaries, comparison)
```

> ⚠️ **Module name vs. directory name.** The package's modules use relative
> imports and the package name is **`claw_zero`** (underscore), but the directory
> is **`claw-zero`** (hyphen). A hyphen is not a valid Python module name, and
> `pyproject.toml` was removed, so `python -m claw_zero` and the `claw_zero`-prefixed
> test imports no longer resolve. See §13.

---

## Phases 1–10 — what shipped (acceptance met)

Each box is checked because its acceptance was verified during the build (tests +
the live smoke test recorded in `memory/session-001.md`). Paths below are the
**post-reorg** locations.

### Phase 1 — Messaging (the equal-operator substrate)
- [x] **1.1 `messaging/mailbox.py`** — `Message` dataclass + `Mailbox` over
  `asyncio.Queue` (`send`/`receive`/`poll`/`has_pending`/`__len__`). FIFO verified.
- [x] **1.2 `messaging/peer.py`** — `Peer` Protocol + `StdioPeer` (human as a peer)
  + `tick_source` coroutine. Nothing in the loop branches on `sender == "human"`;
  the only allowed branch is `kind` (`tick` vs `message`).

### Phase 2 — LLM core
- [x] **2.1 `llm.py`** — one `call()` via
  `AsyncOpenAI().responses.create` returning a normalized `LLMResult`
  (`text`, `tool_calls`, `finish_reason`, `usage`). Folds in the fixed OpenAI
  reasoning setting, OpenAI-only model-id/context-window handling, and
  prompt-boundary stripping.

### Phase 3 — Tools
- [x] **3.1 `tools/bash.py`** — client-side executor for OpenAI local Shell; local
  `asyncio` subprocess in a persistent cwd, middle-truncated output, process-group
  kill on timeout (`start_new_session=True` + `os.killpg`), Claude-Code-rich
  description (shell *is* the file tool).
- [x] **3.2 `tools/registry.py`** — `build_tools` / `get_tool_summaries` split;
  local function-tool specs get handlers; hosted web search and local Shell are
  included as OpenAI Responses tool specs.

### Phase 4 — Prompt builder
- [x] **4.1 `prompt.py`** — gated sections (Identity, Operating loop, Tools,
  Memory, Doing tasks, Faithful reporting, Executing actions with care, Autonomy &
  pacing), byte-stable prefix, volatile runtime context below `CACHE_BOUNDARY`.
  Absent capabilities are simply omitted ("absence is the signal").
- [x] **4.2 `AGENTS.md`** — "home" + two-layer memory ethos; no `DONE`/screenshot
  lines; "peer among peers; reply by sending a message." Injected as a context file.

### Phase 5 — Memory (durable, file-backed)
- [x] **5.1 `memory/store.py`** — `MemoryStore`; layout
  `claw_zero_state/<agent_id>/{AGENT_MEMORY.md, memory/session-NNN.md}`;
  `init_session`/`append_session`/`write_curated`/`read_curated`/`read_file` with
  the `Path.is_relative_to` traversal guard.
- [x] **5.2 `memory/flush.py`** — flush-before-compaction turn via `llm.call`;
  `memory_write(content, target)` routed to the store; token + transcript-byte
  triggers; per-cycle dedup guard.

### Phase 6 — Context & compaction (chat-shape)
- [x] **6.1 `context/token_estimation.py`** — `len(json)/4 * 1.2`; image path removed.
- [x] **6.2 `context/compaction.py`** — preserve last N turns, 0.4 history budget,
  chunked LLM summarize with retry/fallback, **chat-shape** tool_call/tool_result
  pairing repair.
- [x] **6.3 `context/transcript.py`** — append-only JSONL (session / message /
  compaction entries); no image entries.

### Phase 7 — Inner loop (one activation → one delivered message)
- [x] **7.1 `inner_loop.py`** — `run(ActivationContext) -> Message`:
  flush → `llm.call` → dispatch local Shell/function calls → compact-if-over-budget → return the
  plain-text reply as the delivered `Message`. The return value **replaces `DONE`**.
  A 50-iteration backstop guards against a model that never replies.

### Phase 8 — Outer loop (self-owned, never returns)
- [x] **8.1 `outer_loop.py`** — `Agent` (durable cross-activation state) + `run`:
  `receive → append → inner_loop.run → deliver`. `deliver` routes by `recipient`;
  an empty reply is a "sleep" (delivered nothing). Optional self-tick behind a flag.

### Phase 9 — Entry point & config
- [x] **9.1 `config.py`** — `ClawZeroConfig`; only claw-zero's knobs (model,
  context window, compaction threshold, max tool-result chars, tick seconds,
  agent id, base dir). **No effort knob.** Imports nothing heavy.
- [x] **9.2 `__main__.py`** — wires config → mailbox → `StdioPeer` (+ optional
  tick) → memory store → prompt → tools → `outer_loop.run`, with graceful drain on
  stdin EOF. API keys from env only.

### Phase 10 — Verify & document
- [x] **10.1 Smoke test** — passed live against `gpt-5.5` (shell `ls` + a
  memory write via shell + a plain-text reply; loop stayed alive for a follow-up).
- [x] **10.2 README** — `README.md` (now at the repo root) covers the quickstart,
  the two-loop split, the equal-peers model, the local Shell tool, and the
  deferred list.
- [x] **10.3 Tests** — 68 tests pass (`pytest`). They import the `claw_zero`
  package name, which resolves again via the restored `pyproject.toml` (§13.1).
---

## 12. Definition of done 

- [x] A self-owned loop that never exits on its own; a human peer over stdio can
  converse with it; the human is one peer among (future) many — no
  `sender == "human"` special-casing.
- [x] Exactly one client-side command tool: local `shell`.
- [x] An activation ends by **delivering a message**, never by emitting "DONE".
- [x] Durable memory (session log + curated) and flush-before-compaction work;
  long runs compact in place without losing tool pairing.
- [x] No `cua-*` dependency; no computer-use/GUI/image/subagent code.
- [x] At the original milestone, teams were absent by design; cron and a policy
  gate still are. (Teams + in-process A2A messaging shipped later — see §14.)

> The original definition of done was met before the reorg. The reorg introduced
> the §13 packaging gap, which is now the milestone's only outstanding work.

---

## 13. Post-reorg follow-ups — DONE

The `reorganize folders` commit moved the package to the repo root and deleted the
build/run scaffolding. These items restored "it actually runs" — none changed the
design; they were packaging/doc fixes, now complete.

- [x] **13.1 Make the package importable/runnable again.** Restored a
  `pyproject.toml` that maps the `claw_zero` package onto the repo root via
  `[tool.setuptools] package-dir = { "claw_zero" = "." }`, with the subpackages
  (`claw_zero.messaging`/`.tools`/`.context`/`.memory`) listed explicitly — `find`
  can't discover them because the on-disk dirs are `messaging`/`tools`/… and the
  repo dir is `claw-zero` (a hyphen, not a module name). Runtime dep `openai>=2.43`
  and dev dep `pytest>=8` reinstated; console script `claw-zero = claw_zero.__main__:main`.
  **Acceptance met:** `python -m claw_zero --help`, `import claw_zero`, and
  `claw-zero --help` (console script) all run; `import claw_zero` stays lazy (does
  not pull in `openai`).
- [x] **13.2 Restore a dev environment.** `uv venv .venv && uv pip install -e ".[dev]"`
  (or `pip install -e ".[dev]"`). **Acceptance met:** `pip list | grep -i cua` is
  empty; `import openai` succeeds. `.gitignore` now also covers `*.egg-info/`,
  `build/`, `dist/`, and `claw_zero_state/`.
- [x] **13.3 Green tests post-reorg.** **Acceptance met:** `pytest` collects and
  passes all **68** tests (`testpaths = ["tests"]`, `--import-mode=importlib`).
- [x] **13.4 Reconcile the README quickstart.** The root `README.md` `pip install
  -e ".[dev]"` / `python -m claw_zero` quickstart now works as written, and its
  "Layout" block shows the repo-root layout (with `pyproject.toml`, `tests/`,
  `docs/`) instead of the old nested `claw_zero/` tree.

---

## 14. Team of agents — flat peer mesh (SHIPPED)

Extends claw-zero from one self-owned loop to **N loops sharing one bus**, while
keeping the "humans and agents are equal operators" thesis literal. A teammate is
just another participant addressed by id; "message a teammate" and "reply to the
human" are the same `bus.route(...)` call. Drew the *peer-team* model from Claude
Code (`SendMessage`/`TeamCreate`/named teammates/auto-delivery) rather than ALE
Claw's parent→subagent hierarchy, because the flat mesh matches the thesis.

**Design decisions (settled):**
- **Flat peer mesh, no lead, no shared task board.** Any agent can message any
  other agent or the human; coordination is emergent via messages. (No
  Claude-Code-style team lead / task-ownership board — that was the rejected
  alternative.)
- **Both static roster and runtime spawn.** `--agents a,b,c` launches teammates
  at startup; `spawn_agent` brings new ones online mid-run. `--no-spawn` drops
  the spawn tool.
- **In-process only.** Every agent is a coroutine in one event loop. A2A network
  transport stays deferred — the `MessageBus` is the seam it drops into (routing
  is already by recipient id).
- **"Absence is the signal" preserved.** A lone agent (no roster, no spawn) has
  only the baseline tools (`shell` plus hosted `web_search`) and no `# Team`
  prompt section. Team tools/prose appear only when the run is team-capable.
- **Cache-stable gating.** `has_team` (and team-tool registration) is fixed at
  agent creation from config, never from the live peer count — so the `# Team`
  section stays in the byte-stable cached prefix even as `spawn_agent` grows the
  roster.

**What shipped:**
- [x] **14.1 `messaging/bus.py`** — `MessageBus`: per-agent inboxes + external
  peers, `route()` (the single delivery point), `reachable_from()`,
  `has_pending()` drain check. `add_agent` idempotent.
- [x] **14.2 `messaging/peer.py`** — `Peer` now bridges *external* channels into
  the bus (not the mailbox). `StdioPeer.inbound` parses `@id`/`id:` addressing
  against the roster (default recipient otherwise); `tick_source` ticks every
  agent. `parse_address` helper.
- [x] **14.3 `tools/send_message.py`** — DM a peer by id, or `*` to broadcast to
  all teammates (never the human). Does not end the turn. Rejects self/unknown.
- [x] **14.4 `tools/spawn_agent.py`** — create a teammate at runtime (id, optional
  model + opening brief). Thin validator over a `Team`-supplied `spawn` callback
  (avoids a tool→team import cycle).
- [x] **14.5 `team.py`** — `Team` owns the bus, roster, per-agent loop tasks, the
  tick source, runtime spawn, and graceful drain (`run_until_eof` waits for the
  *whole mesh* to settle before teardown).
- [x] **14.6 `outer_loop.py`** — `run(bus, agent)` awaits the agent's own inbox and
  routes replies via the bus; `deliver(reply, bus)`. `Agent.create` takes
  `extra_tools`. `build_system_prompt` passes `has_team` (gated on `send_message`
  registration, not peer count).
- [x] **14.7 `prompt.py`** — gated `# Team` section in the static prefix.
- [x] **14.8 `config.py` / `__main__.py`** — `agents` roster + `allow_spawn`
  knobs; `--agents` / `--no-spawn` CLI; roster validation (no dups, no `human`).
- [x] **14.9 `AGENTS.md`** — "Working with Teammates" coordination ethos.
- [x] **14.10 Tests** — `test_bus.py`, `test_team_tools.py`, `test_team.py` (2
  multi-agent e2e + single-agent-no-team-tools), plus roster/config + prompt
  gating tests. **68 pass.** Live 2-agent smoke test against `gpt-5.5`:
  human→planner→(send_message)→coder→planner→human relay verified.

**Still deferred (unchanged by this milestone):** A2A network transport, a team
lead / shared task board, cron/scheduling, web search, computer use, a
policy/permission gate. Local process supervision for reload is tracked separately
in §15 and should not be confused with remote A2A transport.

---

## 15. Reloadable self-modifying harness — MVP SHIPPED

> **2026-06-28:** MVP shipped. Runtime state persists to JSON, workers resume it,
> completed activations save state, supervisor-managed workers expose
> `reload_harness`, reload requests are auditable, source identity is logged on
> worker start, runtime-spawned teammates are restored, and subprocess smoke
> tests prove a restarted worker sees changed source under a fresh interpreter.
> Source edits are shared by the whole worker/team: learning by one teammate
> applies to all agents after reload because they run from the same codebase.

### 15.0 Motivation

claw-zero can already inspect and edit its own source files via the local Shell
tool, but **editing files is not enough**. The live Python process keeps
already-imported modules, existing class definitions, instantiated `Agent`
objects, tool handlers, closures, and in-memory message state. A code patch made
by the agent may be present on disk while the current harness continues to run the
old code.

That breaks the deeper self-owned-agent goal: claw-zero should be able to improve
or repair its own harness, verify the change, and then continue running under the
new code. The solution is not packaging and not a benchmark-specific fork. The
solution is a **reload/restart boundary**: keep durable agent state outside the
Python object graph, restart a worker process from the source tree, and resume
from the same state.

This is a core harness capability. External benchmarks may motivate it later, but
they must not drive this design. Build the generic reload mechanism first.

### 15.1 Design principles

- **Process boundary, not `importlib.reload()`.** In-process reload is brittle:
  old objects keep old class/function references, partially reloaded dependency
  graphs become inconsistent, and existing tool instances may still point at old
  handlers. A fresh interpreter is the reliable way to pick up changed source.
- **State is external to code.** Conversation state, memory, transcript, pending
  work, and runtime metadata must live in the state directory, not only in Python
  objects.
- **Source-tree execution.** The restarted worker must import from the current
  source tree, not from a stale installed wheel. Editable/package metadata may
  exist for development, but reload correctness should come from launching the
  worker with explicit `cwd`/`PYTHONPATH`/module path.
- **Supervisor is boring and stable.** The supervisor should contain as little
  agent logic as possible: launch worker, preserve args/env/state path, observe a
  reload exit code, and re-exec the worker.
- **No remote transport implied.** This is a local lifecycle boundary. It does not
  change the in-process team/bus model and does not introduce network A2A.
- **No hot-swap mid-activation for MVP.** Reload should occur at a clean boundary:
  after the agent has persisted state and requested reload. Do not try to replace
  live code while tool calls are in flight.
- **Faithful observability.** Reload requests, test results, state save/load, and
  restart counts should be recorded in transcript/session logs so a peer can
  audit what happened.

### 15.2 Target architecture

```text
stable supervisor process
  └── claw-zero worker process, launched from source tree
        ├── loads durable runtime state from --base-dir / --agent-id
        ├── runs normal activation loop
        ├── may edit harness source files using shell
        ├── may run tests/sanity checks
        ├── calls reload_harness(reason=...)
        └── exits with RELOAD_REQUESTED status

supervisor sees RELOAD_REQUESTED
  └── starts a fresh worker with same source tree, cwd, env, model, base-dir,
      agent id, roster, and pending external-peer configuration
```

Suggested constant: `RELOAD_REQUESTED_EXIT_CODE = 75` (or another explicit value;
make it named and tested, not magic).

### 15.3 Durable state that must survive reload

Current claw-zero already persists some state (`AGENT_MEMORY.md`, session logs,
transcript), but a reloadable worker also needs to persist/restore the runtime
state that is currently in memory.

Minimum single-agent state:

- [x] **Conversation messages.** Persist the chat-shaped `agent.messages` list,
  including assistant `response_items`, function `tool_calls`, tool outputs, and
  shell outputs exactly enough for the next Responses call to be valid.
- [x] **Flush/compaction state.** Persist `FlushState` fields so reload does not
  reset memory-flush bookkeeping or duplicate pre-compaction flushes.
- [x] **Token accounting.** Persist `last_api_input_tokens` and any context-window
  / auto-compaction settings used to interpret the current state.
- [x] **Runtime config.** Persist or reconstruct model, agent id, operator id,
  cwd, base dir, context window overrides, tick settings, tool-output limits, and
  team/spawn flags.
- [x] **Memory/transcript paths.** Preserve existing file layout and append to the
  same transcript/session log after reload; do not start a hidden new run unless
  explicitly logged.
- [x] **Reload metadata.** Persist a small `reload_state.json` or equivalent with
  requested reason, requesting agent id, timestamp, restart count, source tree,
  argv/env fingerprint, and last completed state-save id.

Team/multi-agent follow-up state:

- [x] **Roster.** Persist static roster plus spawned teammates and their models.
- [x] **Per-agent state.** Persist each agent's messages, flush state, token
  accounting, context files, and memory path separately.
- [x] **Pending inboxes.** Decide whether reload waits for the mesh to become idle
  before exiting (preferred MVP) or serializes pending bus messages. If serializing,
  include sender, recipient, kind, content, and ordering. MVP requests reload at
  the tool-call boundary and persists per-agent state; pending inbox
  serialization remains a later extension.
- [x] **External peers.** Preserve operator id / stdio addressing configuration;
  do not duplicate peer bridges across restarts.

### 15.4 New components / changes to implement

- [x] **15.4.1 Runtime state serializer.** Add a small, explicit serializer for
  `Agent` runtime state. Keep it boring JSON under the agent state dir. Avoid
  pickles; source changes must not break state loading just because a class moved.
- [x] **15.4.2 Runtime state loader.** Add `Agent.load(...)` or a companion
  function that reconstructs an `Agent` from durable state plus current source
  code. Fresh tool instances should be built from the new code on every worker
  start.
- [x] **15.4.3 Save-at-boundaries.** Save runtime state after each activation and
  immediately before reload. This makes reload and crashes use the same recovery
  path.
- [x] **15.4.4 Supervisor entrypoint.** Add a stable supervisor mode/entrypoint
  that starts a worker subprocess with explicit args/env/cwd and handles normal
  exit vs reload-request exit vs failure.
- [x] **15.4.5 Worker entrypoint.** Split the current CLI enough that the worker
  can run under supervision while preserving existing `python -m claw_zero`
  behavior. The worker owns the actual agent/team loop.
- [x] **15.4.6 `reload_harness` tool.** Register this tool only when running under
  a reload-capable supervisor. Parameters should include at least `reason` and
  optionally `tests_run` / `summary`. The tool should:
  1. append an auditable transcript/session-log entry,
  2. save all runtime state,
  3. write reload metadata,
  4. request clean worker shutdown with the reload exit code.
- [x] **15.4.7 Clean shutdown path.** Avoid `os._exit` except as a last-resort
  fallback. Prefer raising/propagating a typed reload signal to the top-level
  worker so `finally` blocks, transcript flushes, and subprocess cleanup can run.
- [x] **15.4.8 Restart loop guard.** Supervisor should cap repeated reloads or
  failures (for example max N reloads per M minutes) and report faithfully rather
  than spinning forever on broken code.
- [x] **15.4.9 Source identity logging.** On worker start, log source root, git
  commit if available, dirty status summary, argv, model, and state dir. After
  reload, record that the new worker picked up code from disk.
- [x] **15.4.10 Backward compatibility.** Existing single-process CLI should keep
  working. If no supervisor is present, `reload_harness` should be absent or
  return a clear unsupported error; absence is preferred.

### 15.5 Prompt/tool contract updates

- [x] **Prompt: explain reload capability only when available.** Add a gated
  prompt section saying the agent may edit harness code, run checks, and call
  `reload_harness` to pick up changes. Do not show this section when unsupported.
- [x] **Prompt: require verification disclosure.** The agent must report whether
  it ran tests before requesting reload and must not imply tests passed if they
  were not run.
- [x] **Tool description: clear semantics.** `reload_harness` does not complete
  the current peer task by itself. It requests a restart; after reload, the agent
  should continue or report back using restored state.
- [x] **Tool description: clean-boundary warning.** The tool should be used after
  source edits and verification, not casually inside ordinary task solving.

### 15.6 Testing plan

Unit tests:

- [x] Serialize/load a single `Agent` with user messages, assistant tool calls,
  tool outputs, shell outputs, `FlushState`, and `last_api_input_tokens`; loaded
  state should produce equivalent next API input shape.
- [x] Serializer uses JSON-compatible primitives only; no pickle/class-object
  dependency.
- [x] `reload_harness` is absent when not supervised, or returns a deterministic
  unsupported result if intentionally registered.
- [x] `reload_harness` writes reload metadata and causes the worker to choose the
  reload exit code through a typed signal.
- [x] Supervisor restarts on reload exit code and does not restart on normal exit.
- [x] Supervisor enforces restart-loop guard.

Integration tests / smoke tests:

- [x] **Marker reload smoke.** Add a tiny harness-visible marker/version function.
  Run a supervised worker, edit the marker source, restart on the reload exit
  code, and verify the restarted worker reports the new marker value.
- [x] **Tool reload smoke.** Change a toy tool's behavior on disk, reload, and
  verify the new tool handler is used (proves fresh tool instances come from new
  code).
- [x] **State continuity smoke.** Before reload, store a fact in conversation and
  durable state; after reload, verify it is still available from the same state
  path.
- [x] **Team later.** Once single-agent reload works, repeat with two agents and a
  pending/idle mesh decision. Do not block the single-agent MVP on team reload.

### 15.7 Acceptance criteria for the MVP

- [x] A supervised run can edit source code, request reload, and resume from the
  same state directory under a fresh interpreter.
- [x] The post-reload worker demonstrably uses changed source code, not old module
  objects.
- [x] Conversation history, transcript append path, session memory, flush state,
  and token accounting survive the restart.
- [x] Existing unsupervised `python -m claw_zero` behavior still works.
- [x] Existing tests pass, plus new reload serializer/supervisor tests pass.
- [x] Reload attempts are auditable in transcript/session logs.
- [x] A broken reload does not spin forever; failure is reported with the relevant
  exit code/output.

### 15.8 Later extensions, not MVP

- Pending inbox serialization for reloads requested while messages are queued.
- Live upgrade while preserving long-running background ticks.
- Versioned state migrations for larger schema changes.
- Remote/network A2A transport.
- Benchmark-specific recovery policies. Keep those outside the core reload design
  until the generic mechanism is working.

---
## Deferred by design

These are intentionally **absent** (TODO markers, not built):

- computer use / GUI, images, vision
- **remote/network A2A transport** — the team is currently in-process and there
  are no external/remote agents. The local supervisor/worker reload boundary in
  §15 is a lifecycle mechanism, not remote transport.
