# claw-zero — Implementation TODO / Status

> **Status: built, reorganized, and re-packaged — runnable again.** All 11 phases
> (0–10) were implemented, committed, and verified — 50 tests green and a live
> smoke test against `openai/gpt-5.5` (see `memory/session-001.md`). A subsequent
> commit (`fca034b "reorganize folders"`) **flattened the package from
> `claw_zero/` into the repo root** and removed the in-repo `harness/` and
> `legacy/` snapshots, `pyproject.toml`, and the per-package `PORTING.md`/`README.md`.
> That reorg left the package un-runnable as `python -m claw_zero`; **§13
> Post-reorg follow-ups** restored packaging (a `pyproject.toml` that maps the
> `claw_zero` package onto the repo root) — `python -m claw_zero` runs and all 50
> tests pass again. No open work remains.

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
| 2 | LLM core (`llm.py` via litellm) | ✅ shipped |
| 3 | Tools — exactly one (`bash`, `registry`) | ✅ shipped |
| 4 | Prompt builder + `AGENTS.md` | ✅ shipped |
| 5 | Durable memory (`store`, `flush`) | ✅ shipped |
| 6 | Context & compaction (chat-shape) | ✅ shipped |
| 7 | Inner loop (one activation → one message) | ✅ shipped |
| 8 | Outer loop (self-owned, never returns) | ✅ shipped |
| 9 | Entry point & config | ✅ shipped |
| 10 | Verify & document | ✅ shipped (50 tests green, live smoke test) |
| 13 | Post-reorg follow-ups | ✅ done — `pyproject.toml` restored; runnable, 50 tests pass |

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
- **LLM client:** `litellm`. Keep LiteLLM model-string format so Anthropic /
  OpenAI / Bedrock / Vertex all work. **No** additional SDK.
- **Dropped completely:** `cua-agent`, `cua-computer`, `cua-core`, `Pillow`, all
  computer-use / GUI / screenshot / VM code, the `canonical/` and
  `model/unified_loop.py` SDK-bridge layers, image handling, and `subagent/`
  delegation.
- **Tools (minimal):** exactly one — `bash` (client-side, local subprocess). It is
  also the file tool (`cat`/`grep`/`find`/`sed`/`python -c`). No dedicated
  read/write/edit/grep/glob tools, no web search, no policy/permission gate.
- **Messaging transport:** in-memory mailbox; **human is just a peer over stdio**,
  behind an interface so a real agent-to-agent (A2A) transport drops in later.
  **A2A substrate is explicitly deferred — not built.**
- **Outer loop vs inner loop are separate modules.**

**Ground-truth facts (settled — do not "correct" these):**
- Default model: `openai/gpt-5.5` (LiteLLM format).
- Thinking/effort is **always max** wherever it applies. `llm.py` folds in the
  ported thinking layer and passes `MAX_EFFORT` (`ThinkLevel.XHIGH`) on every call
  site (main loop, compaction, memory flush). There is **no per-site effort knob**.
- Prompt caching is a **prefix match**: the system prompt prefix is byte-stable;
  volatile content sits below `CACHE_BOUNDARY` (`<!-- CLAW_ZERO_CACHE_BOUNDARY -->`).
  `llm.apply_cache_markers` handles the markers.

**Out of scope for this milestone (TODO markers only — not built):**
computer use, GUI, images, web search, subagent delegation, A2A network transport,
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
  llm.py                      # single litellm call + thinking + model resolve + cache (Phase 2)
  prompt.py                   # gated "absence is the signal" builder (Phase 4)
  AGENTS.md                   # persistent operating doc (Phase 4)
  outer_loop.py               # self-owned loop + Agent (Phase 8)
  inner_loop.py               # one activation → one delivered Message (Phase 7)
  messaging/
    mailbox.py                # Mailbox + Message (Phase 1)
    peer.py                   # Peer + StdioPeer + tick_source (Phase 1)
  tools/
    registry.py               # build_tools / get_tool_summaries (Phase 3)
    bash.py                   # the single tool (Phase 3)
  context/
    compaction.py             # chat-shape compaction (Phase 6)
    token_estimation.py       # chars/4 estimator, no image path (Phase 6)
    transcript.py             # append-only JSONL (Phase 6)
  memory/
    store.py                  # MemoryStore (Phase 5)
    flush.py                  # flush-before-compaction (Phase 5)
    session-001.md            # the build's own dog-food log
  tests/                      # 50 tests (still import the `claw_zero` package name)
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
- [x] **2.1 `llm.py`** — one `call()` via `litellm.acompletion` returning a
  normalized `LLMResult` (`text`, `tool_calls`, `finish_reason`, `usage`). Folds in
  thinking/effort (always `MAX_EFFORT`), model resolution, and the cache policy.

### Phase 3 — Tools (exactly one)
- [x] **3.1 `tools/bash.py`** — client-side `bash(command, timeout?)`; local
  `asyncio` subprocess in a persistent cwd, middle-truncated output, process-group
  kill on timeout (`start_new_session=True` + `os.killpg`), Claude-Code-rich
  description (bash *is* the file tool).
- [x] **3.2 `tools/registry.py`** — `build_tools` / `get_tool_summaries` split;
  exactly one spec + one handler; summaries consumed by the prompt builder.

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
  flush → `llm.call` → dispatch `bash` → compact-if-over-budget → return the
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
- [x] **10.1 Smoke test** — passed live against `openai/gpt-5.5` (bash `ls` + a
  memory write via bash + a plain-text reply; loop stayed alive for a follow-up).
- [x] **10.2 README** — `README.md` (now at the repo root) covers the quickstart,
  the two-loop split, the equal-peers model, the single `bash` tool, and the
  deferred list.
- [x] **10.3 Tests** — 50 tests pass (`pytest`). They import the `claw_zero`
  package name, which resolves again via the restored `pyproject.toml` (§13.1).

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
  repo dir is `claw-zero` (a hyphen, not a module name). Runtime dep `litellm>=1.80`
  and dev dep `pytest>=8` reinstated; console script `claw-zero = claw_zero.__main__:main`.
  **Acceptance met:** `python -m claw_zero --help`, `import claw_zero`, and
  `claw-zero --help` (console script) all run; `import claw_zero` stays lazy (does
  not pull in `litellm`).
- [x] **13.2 Restore a dev environment.** `uv venv .venv && uv pip install -e ".[dev]"`
  (or `pip install -e ".[dev]"`). **Acceptance met:** `pip list | grep -i cua` is
  empty; `import litellm` succeeds. `.gitignore` now also covers `*.egg-info/`,
  `build/`, `dist/`, and `claw_zero_state/`.
- [x] **13.3 Green tests post-reorg.** **Acceptance met:** `pytest` collects and
  passes all **50** tests (`testpaths = ["tests"]`, `--import-mode=importlib`).
- [x] **13.4 Reconcile the README quickstart.** The root `README.md` `pip install
  -e ".[dev]"` / `python -m claw_zero` quickstart now works as written, and its
  "Layout" block shows the repo-root layout (with `pyproject.toml`, `tests/`,
  `docs/`) instead of the old nested `claw_zero/` tree.

---

## 12. Definition of done (the original milestone — MET)

- [x] A self-owned loop that never exits on its own; a human peer over stdio can
  converse with it; the human is one peer among (future) many — no
  `sender == "human"` special-casing.
- [x] Exactly one client-side tool: `bash`.
- [x] An activation ends by **delivering a message**, never by emitting "DONE".
- [x] Durable memory (session log + curated) and flush-before-compaction work;
  long runs compact in place without losing tool pairing.
- [x] No `cua-*` dependency; no computer-use/GUI/image/subagent code.
- [x] A2A transport, teams, cron, and any policy gate are **absent by design**.

> The original definition of done was met before the reorg. The reorg introduced
> the §13 packaging gap, which is now the milestone's only outstanding work.

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
