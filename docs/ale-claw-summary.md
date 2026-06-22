# ALE Claw — Harness Summary

> **What it is.** A Python-native, intentionally minimal agent harness for the
> *Agents' Last Exam* (ALE) computer-use benchmark, inspired by the OpenClaw
> agent architecture. It drives a remote desktop VM (via the Cua Agent SDK) and
> runs a single long-horizon action loop until the task is done or the turn
> budget is exhausted. This is the codebase living in this repo
> (`config.py`, `deployer.py`, `harness/`).

The design thesis: *a small harness with the right loop, tools, memory, and
context management can perform as well as much heavier agent platforms while
spending far fewer tokens, dollars, and minutes.* The model does most of the
work; the harness stays narrow on purpose.

---

## 1. The Main Loop (`harness/agent_loop.py`, ~1109 LOC)

The core is `OpenClawComputerAgent.run()` (around `agent_loop.py:193`). It
overrides the Cua SDK's `ComputerAgent.run()` with a **mutable items list** so
context can be compacted *in place* without rebuilding the agent.

Per-turn flow:

| Step | What happens | Where |
|------|--------------|-------|
| 0 | `should_continue` check (turn budget guard) | `agent_loop.py:231` |
| 1 | Combine `items + new_items`, sanitize runtime messages | `:235` |
| 2 | `_on_llm_start()` updates `current_tokens` estimate | `:238` |
| 3 | **Pre-API memory flush** (`_maybe_flush_memory()`) — token/byte thresholds + dedup guard | `:243` |
| 4 | `predict_step()` — the LLM call, wrapped in a **reactive overflow** try/except | `:256` |
| 5 | Yield result; log to transcript; print bare agent text | `:294`–`302` |
| 6 | Append output to `new_items`; yield partial items | `:304` |
| 7 | Drain completed subagent results + post-GUI-delegation screenshot | `:328`, `:332` |
| 8 | **Proactive compaction** if `overflow_cb.needs_compaction` | `:335` |
| 9 | **DONE termination** if `has_done_signal(output)` | `:345` |
| 10 | **Bare-text nudge** if a turn produced neither tool call nor DONE | `:353` |

**Termination & budget**
- Hard ceiling: `max_steps` (config `max_turns`, default **100**).
- Soft exit: the model emits **`DONE`** on its own line. Detected by
  `has_done_signal()` (`agent_loop_helpers.py:84`) using regex
  `^\s*\**DONE\**(?:[:\s]+(.*))?$` — so "JOB DONE" does *not* trigger it.
- Exception exit: an unrecoverable error bubbles to the deployer, which logs and
  returns a failed status.

**Error resilience**
- *Truncated tool-call arguments* (provider drops mid-stream) are repaired by
  `_sanitize_truncated_function_calls()` — invalid JSON is rewritten to a
  placeholder and a synthetic `function_call_output` error is emitted so the
  next compaction re-parse doesn't crash.
- *Context overflow* mid-API is caught and triggers a forced compaction + retry
  (reactive path), complementing the proactive between-turns check.

---

## 2. System Prompt Construction (`harness/prompt.py`, 602 LOC)

`PromptBuilder.build()` assembles the system prompt from **composable, gated
sections**. The governing principle (borrowed from OpenClaw) is
**"absence is the signal"**: if a tool isn't registered, its prose section is
simply not emitted — the model is never told "X is disabled."

Section order (`prompt.py:159`):

1. **Identity** — `## Identity` (always)
2. **Tools** — `## Tools` list of `- **name**: description` (if tools present)
3. **Shell Execution** — `## Shell Execution` (only if `exec` registered)
4. **Memory Recall** — `## Memory Recall` (only if memory tools present)
5. **Delegation** — `## Delegation` (only if `delegate_general`/`delegate_gui`)
6. **Current Date & Time** — `## Current Date & Time` (UTC)
7. **Project Context** — `# Project Context` (bootstrap-injected files)

### Verbatim: Identity (`prompt.py:200`)

```
## Identity

You are an AI agent running inside the AgentHLE benchmark framework. Your role
is to complete computer-use tasks on a remote {Windows|macOS|Linux} desktop VM
by observing screenshots and performing mouse/keyboard actions.
```

The OS label is resolved from the sandbox's `target_os`; unknown → generic
"remote desktop VM (Windows or Linux)".

### Memory Recall (`prompt.py:290`) — subset-branched

The prose changes depending on which of `memory_search` / `memory_get` /
`write` are present. With both search and get:

```
## Memory Recall
Before acting on anything about prior attempts, strategies, environment
observations, or task state: run memory_search on TASK_MEMORY.md +
memory/session-*.md; then use memory_get to pull only the needed lines. If low
confidence after search, say you checked.
Citations: include Source: <path#line> when referencing memory snippets.
Writing: use write with target='host' to journal memory. Append raw
observations, actions, and errors to memory/session-NNN.md during the run.
Update TASK_MEMORY.md with distilled strategies and patterns worth keeping
across sessions.
```

### Delegation (`prompt.py:348`) — see §5 for full text.

### Project Context / bootstrap injection (`prompt.py:468`)

Context files (AGENTS.md, optionally TASK_MEMORY.md) are injected verbatim under
`# Project Context`, subject to budget caps:
- Per file: `BOOTSTRAP_MAX_CHARS = 12_000`
- Total: `BOOTSTRAP_TOTAL_MAX_CHARS = 60_000`
- Truncation: **head 70% / tail 20%** split with an inline `[...truncated...]`
  marker (`prompt.py:68`).

### `AGENTS.md` — the persistent "constitution" (`harness/AGENTS.md`)

Loaded fresh each launch and injected as a context file. Its themes:
- **"This task environment is home. Treat it that way."**
- **Memory** — two layers: append-only `session-NNN.md` scratchpad + curated
  `TASK_MEMORY.md`. *"Write It Down — No 'Mental Notes'!"*
- **Task Completion** — *"output **DONE** on its own line ... verify your work
  by checking the screen first."*
- **General Behavior** — observe before acting, try alternatives on failure,
  avoid destructive actions.

> **Authoring rule** (documented in `prompt.py:5`): tool-specific operational
> rules live in gated `_build_<tool>()` methods, **not** in AGENTS.md — because
> AGENTS.md is injected into every prompt, so putting tool prose there would
> leak a disabled tool's instructions.

---

## 3. Context Management (`harness/context/`)

### Transcript
Append-only JSONL at `<base_dir>/<task_id>/transcript.jsonl`. Entry types:
`session` (header: version, task_id, run, model), `message` (role/content/usage/
stopReason, parent chain), and `compaction` (summary, firstKeptEntryId,
tokensBefore).

### Token estimation (`context/token_estimation.py`)
- Heuristic: `tokens ≈ len(json.dumps(msg)) / 4`.
- Images: subtract the base64 chars, add `FIXED_IMAGE_TOKENS = 1200` per image.
- Apply `SAFETY_MARGIN = 1.2`.

### Overflow detection & tool-result truncation (`context/context.py`)
- `DEFAULT_CONTEXT_TOKENS = 200_000` fallback.
- Proactive `ContextOverflowCallback`: on each `_on_llm_start`, estimate tokens;
  set `needs_compaction` when estimate > `context_window × threshold` (~0.8).
- Per-result clamps: `MAX_TOOL_RESULT_SHARE = 0.25` and
  `HARD_MAX_TOOL_RESULT_CHARS = 16_000`, head+tail truncation that preserves
  error/completion lines (`MIN_KEEP_CHARS = 2_000`).
- Reactive: `is_context_overflow_error()` matches API rejections
  ("context_length_exceeded", "max_tokens", "token_limit").

### Compaction (`context/compaction.py`) — LLM summarize + truncate
`compact_messages()`:
1. Preserve the last N complete turns unconditionally.
2. Budget the "kept" history at `context_window × max_history_share` (default
   **0.40**) and prune until it fits.
3. Summarize older history via an LLM, chunked by token share
   (`BASE_CHUNK_RATIO = 0.40`, floor `MIN_CHUNK_RATIO = 0.15`), with
   `SUMMARIZATION_OVERHEAD_TOKENS = 4096`, up to `MAX_SUMMARIZATION_RETRIES = 3`,
   timeout 120s, fallback summary on failure.
4. Repair orphaned tool_use/tool_result pairs.
5. Return a `CompactionResult` (summary, first_kept_message_index, tokens_before).

`_compact_in_place()` (`agent_loop.py:913`) rebuilds the mutable items list from
the compacted state, resets the overflow callback, and re-anchors a
post-compaction message (re-injecting context files).

### Image retention (`adapters/image_retention.py`)
- Mode `"openclaw"` (default): keep all images from the last N **completed
  turns** (cache-friendly, sticky-placeholder replacement → less cache thrash).
- Mode `"cua"`: keep the last N images by raw count.

---

## 4. Memory Subsystem (`harness/memory/`)

**Layout**
```
<base_dir>/tasks/<task_id>/
├── TASK_MEMORY.md            # curated, bootstrap-injected
└── memory/
    ├── session-001.md        # append-only session log
    └── session-002.md ...
```

**`MemoryStore` API** (`memory/memory.py`): `init_session()`,
`append_to_session_log()`, `write_task_memory()` (full-file overwrite),
`read_task_memory()`, `read_file(path, start, end)`. Path-traversal is guarded
with `Path.is_relative_to`.

**Memory flush** — a *pre-compaction* LLM turn that writes durable memory before
old context is summarized away (`memory/memory_flush.py`, called from
`agent_loop.py` before `predict_step`). Triggered (`memory_flush_policy.py`) when
*either*:
- token count ≥ `compaction_trigger − reserve − soft_threshold`
  (`SOFT_THRESHOLD = 4000`, `RESERVE_FLOOR = 20_000`), **or**
- transcript file ≥ `FORCE_TRANSCRIPT_BYTES = 2 MB`.

A dedup guard (`has_already_flushed_for_current_compaction`) prevents repeat
flushes within one compaction cycle. The flush LLM is told:

```
Pre-compaction memory flush turn. Capture durable memories. Usually [!silent]
is correct.
```
and replies `[!silent]` when there's nothing worth saving.

---

## 5. Subagents / Delegation (`harness/subagent/`)

Two delegate types, plus a control tool. Full verbatim prose
(`prompt.py:348`):

> ### `delegate_general(task, ...)` — async, auto-announces
> Spawns a general-purpose subagent session that has **no VM access** — only
> memory tools and LLM reasoning. ... Returns immediately with
> `{status: accepted, run_id, note}`. Keep working — **do NOT poll**. When the
> subagent finishes, its result is injected automatically as a
> `[Subagent Result]` user message on a later turn. ... (cap: **3 active**).
>
> ### `delegate_gui(instruction, ...)` — async, auto-announces
> Spawns a GUI automation subagent driven by a vision model. It takes over the
> VM for a bounded number of steps (default **15**) ... **do not call
> `delegate_gui` again or use `computer` directly until it completes**.
>
> ### `subagents(action=list | kill | steer, target=..., message=...)`
> - `list` — active + recent runs. **Do NOT poll**.
> - `kill` — cancel a runaway general subagent.
> - `steer` — inject a message (max **4000 chars**) into a running subagent.
>
> ### Rules of thumb
> - Don't delegate trivial things ... don't sit idle waiting ... **don't nest
>   delegation**.

**Registry** (`subagent/subagent_registry.py`): lifecycle `PENDING → RUNNING →
{COMPLETE | ERROR | KILLED}`, in-memory dict + append-only
`subagents-runs.jsonl` persistence, FIFO `drain_completions()` /
`drain_post_delegation()`. Subagent transcripts land at
`<parent_session_dir>/subagents/<run_id>/transcript.jsonl`.

Delegation tools (`subagent/subagent_tools.py`) validate the requested model
against a small allowlist (`[default, auxiliary]`) so a hallucinated sibling id
can't reach litellm. General delegates run as background asyncio tasks; GUI
delegates run blocking in a thread pool.

---

## 6. Tools (`harness/tools/`)

Assembled by `build_tools()` (`tools/tools.py:151`). Default surface:

| Tool | Verbatim one-line description |
|------|-------------------------------|
| `computer` | "Observe the current desktop via screenshots and interact with it using mouse and keyboard actions. Only the explicit `screenshot` action returns an image..." |
| `analyze_image` | "Analyze one or more images with a vision model and return a text description... Returns text only — no images are added to your context." |
| `read` | "Read a file. Pick a filesystem via `target` (default 'vm'). Text files return line-paginated content; image files... return the raw image..." |
| `write` | "Create, overwrite, or append to a UTF-8 text file. Pick a filesystem via `target` (default 'vm'). Default `append` behavior is target-aware: vm overwrites, host appends." |
| `edit` | "Make precise edits to a file. Pick a filesystem via `target` (default 'vm'). Each `{oldText, newText}` replacement must match exactly." |
| `exec` | "Run a single non-GUI shell command inside the remote VM and return stdout/stderr/exit_code. cmd.exe on Windows, /bin/sh on POSIX. GUI apps block until they exit — use the computer tool for GUI work." |
| `web_search` | "Search the web (Brave API). Returns ranked results with title, url, and description." *(disabled by default — needs `BRAVE_API_KEY`)* |
| `web_fetch` | "Fetch and extract readable text from an HTTP(S) URL. Use for lightweight page access without browser automation." |
| `memory_search` | "Search task memory files (TASK_MEMORY.md and session logs) by keywords... Returns matched lines with file path and line number." |
| `memory_get` | "Read a memory file... with optional line range. Use after memory_search to pull only the needed lines and keep context small." |
| `delegate_general`, `delegate_gui`, `subagents` | (see §5) |

Notable design choices:
- **Layered descriptions.** Layer 1 = the one-line `BaseTool.description` (above).
  Layer 2 = non-obvious operational rules, emitted only via gated prompt-builder
  sections (e.g. the entire `## Shell Execution` block lives in
  `_build_exec()`, not on the tool).
- **`target=` filesystem routing.** `read`/`write`/`edit` take a `target`
  (`vm` | `host`) so the same vocabulary spans the VM and the host workspace.
  `host` is only registered (and only appears in the schema enum) if a valid
  `host_workspace_root` exists.
- **Transport abstraction.** Non-GUI tools reach the VM via MCP (`vm_mcp_server`)
  or legacy session RPC; GUI reaches the VM via `cua_mcp_server` or session.
  Same tool granularity either way (`config.py` `substrate_transport` /
  `gui_transport`).
- **`memory_write` is hidden from the main agent** — its journaling role is
  covered by `write(target='host')`; the class survives only for `memory_flush`
  and the GUI subagent.

---

## 7. Model Layer (`harness/model/`)

- **Thinking levels** (`thinking.py`): `off | low | medium | high` per call-site
  (main / flush / compaction / vision / gui). Vision and GUI default to `off`
  for cost. Defaults resolve per model family (e.g. Claude 4.6 → adaptive).
  Provider mapping: Anthropic `thinking.budget_tokens`, OpenAI `reasoning.effort`,
  Gemini `thinking_level`, fallback `reasoning_effort`.
- **Cache policy** (`cache_policy.py`): prompt caching on by default; cache
  markers placed on stable blocks (system prompt, post-compaction context).
- **Unified loop** (`unified_loop.py`): a function-calling `computer` tool path
  for any `openrouter/*` model, so a single loop serves many provider backends.
- **Model config registry** (`model_config.py`): declarative capability metadata
  (tool schema type, screenshot output type, batched vs single actions, adapter
  target, context window) keyed by regex on the model id.

---

## 8. Config Knobs That Matter (`config.py`)

| Knob | Default | Meaning |
|------|---------|---------|
| `model` | `openrouter/anthropic/claude-sonnet-4.6` | main model (LiteLLM id) |
| `max_turns` | `100` | hard step ceiling (→ `max_steps`) |
| `summary_model` / `gui_model` / `auxiliary_model` | `None` | cheaper siblings for compaction/flush, GUI, delegation |
| `disabled_tools` | `["web_search"]` | tools dropped from the list |
| `disable_main_computer` / `disable_delegate_gui` | `False` | force GUI through delegation, or forbid GUI delegation |
| `substrate_transport` / `gui_transport` | `mcp` | how tools reach the VM |
| `thinking_level` (+ per-site overrides) | `None` (resolved) | reasoning effort |
| `image_retention_mode` | `openclaw` | screenshot retention strategy |

---

## 9. Strengths to carry forward (for the non-user-facing loop)

1. **Genuinely minimal, self-contained loop** — already a single long-horizon
   action loop with no product/UI layer. This is the closest existing match to
   the target "self-owned, long-running" agent.
2. **`DONE`-signal termination** — clean, model-driven stop condition.
3. **Memory-flush-before-compaction** — durable memory survives context loss; a
   strong pattern for very long runs.
4. **"Absence is the signal" gated prompt** — prompt prose tracks the actual
   tool list with no drift.
5. **Two-layer memory** (raw session log + curated TASK_MEMORY.md) with explicit
   "write it down" discipline.
6. **In-place compaction** on a mutable items list — no agent rebuild.

## 10. Gaps relative to Claude Code

- Tool descriptions are *terse* (one line); Claude Code's are multi-paragraph
  with examples and anti-patterns.
- No inter-agent messaging primitive beyond parent↔subagent delegation (the
  target system wants **agent-to-agent** communication).
- No autonomous "tick"/wake-up pacing loop — ALE Claw runs to completion on a
  single task rather than living indefinitely.
- Single hierarchical delegation only (no peer teams, no cron/scheduling).
