# PORTING.md — harness → claw-zero (KEEP / PORT / DROP)

claw-zero is **self-contained**: it does not import `harness.*` (the harness
`__init__.py` eagerly pulls the cua SDK — `import agent` — so reusing it would
re-introduce the dependency we are dropping). So everything we keep is **ported
verbatim or adapted** into `claw_zero/`, with no cua / computer-use / image /
subagent code. Below, **PORT** means "copied into `claw_zero/` and trimmed";
**DROP** means "not carried over."

One structural adaptation runs through the whole port: the harness loop speaks
the **CUA canonical message shape** (`content` = a list of typed blocks:
`function_call`, `computer_call`, `tool_result`, …). claw-zero's loop (Phase 7)
speaks the **OpenAI chat shape** litellm returns directly — assistant messages
carry `tool_calls`, and tool results are separate `{"role": "tool",
"tool_call_id": ...}` messages. So compaction's tool-pairing repair and the
flush/summary serializers are **re-pointed at the chat shape**, not copied
block-for-block.

| Source file (`harness/`) | Action | Destination / Notes |
|---|---|---|
| `model/thinking.py` | **PORT** | `claw_zero/llm.py` (folded in). Keep `ThinkLevel`, the provider mapping (`resolve_thinking_params`), and the budgets. claw-zero always calls at **max effort** (`ThinkLevel.XHIGH`) on every call site. |
| `model/model_config.py` | **PORT (trim)** | `claw_zero/llm.py` (folded in). Keep `resolve_model` / provider inference / `context_window` lookup. Drop the computer-use format fields (`tool_schema_type`, `screenshot_output_type`, `action_format`, `adapter_target`, safety checks) — no GUI. |
| `model/helper_runtime.py` | **PORT** | `claw_zero/llm.py`. The single litellm call (chat + responses transports, tool-call extraction). This *is* `llm.call`. |
| `model/cache_policy.py` | **PORT** | `claw_zero/llm.py` (folded in). Sliding `cache_control` breakpoints + `<!-- CLAW_ZERO_CACHE_BOUNDARY -->`. Byte-stable prefix; volatile content below the boundary. |
| `model/unified_loop.py` | **DROP** | cua `register_agent` / computer-tool path. |
| `model/_message_shapes.py`, `image_sanitization.py` | **DROP** | cua/image plumbing. |
| `context/token_estimation.py` | **PORT (trim)** | `claw_zero/context/token_estimation.py`. Keep `len(json)/4 * SAFETY_MARGIN`. **Remove** the `FIXED_IMAGE_TOKENS` / base64 path. |
| `context/compaction.py` | **PORT (adapt)** | `claw_zero/context/compaction.py`. Keep preserve-last-N, 0.4 history budget, chunked LLM summarize, retry/fallback. **Adapt** `repair_tool_use_result_pairing` and the serializers from canonical blocks → chat shape. |
| `context/context.py` | **PORT (subset)** | Truncation helpers (`truncate_tool_result_text`, head+tail, important-tail) → folded where needed; the `ContextOverflowCallback` cua-callback class is **DROP** (no cua callback chain — the loop checks the budget inline). |
| `context/transcript.py` | **DROP** | cua step-output grouping (`group_step_output`). Not relevant to chat shape. |
| `context/replay.py` | **DROP** | cross-run replay/sanitize; claw-zero keeps live in-memory messages. |
| `session.py` (`SessionManager` JSONL) | **PORT (trim)** | `claw_zero/context/transcript.py` — the **append-only JSONL writer** (session / message / compaction entries) lives here. Drop cross-run state.json, replay, image entries. |
| `memory/memory.py` (`MemoryStore`) | **PORT (trim)** | `claw_zero/memory/store.py`. Keep layout, `init_session`/append/curated/read + `is_relative_to` guard. Rename `TASK_MEMORY.md`→`AGENT_MEMORY.md`; layout `claw_zero_state/<agent_id>/`. **Drop** the `BaseTool` memory tools (no memory tool — memory is reached via bash). |
| `memory/memory_flush.py` | **PORT (adapt)** | `claw_zero/memory/flush.py`. Pre-compaction flush turn via `llm.call`; `memory_write(content, target)` routed to the store. Serialize chat-shape history. |
| `memory/memory_flush_policy.py` | **PORT** | `claw_zero/memory/flush.py` (folded in). Token + transcript-byte triggers, dedup guard. |
| `prompt.py` (`PromptBuilder`) | **PORT (rewrite prose)** | `claw_zero/prompt.py`. Keep the gated "absence is the signal" assembly + bootstrap injection + cache boundary. **Rewrite** all prose for a non-user-facing peer agent; import Claude Code's behavior/altitude/autonomy sections. |
| `AGENTS.md` | **PORT (edit)** | `claw_zero/AGENTS.md`. Keep "home" + two-layer memory ethos. **Delete** the `DONE` / screenshot lines; add "peer among peers; reply by sending a message." |
| `agent_loop.py` (`OpenClawComputerAgent`) | **DROP / rewrite** | The cua `ComputerAgent` subclass is dropped. Its loop *logic* (flush → call → tools → compact) is **rewritten** as `claw_zero/inner_loop.py` + `outer_loop.py`. |
| `tools/tools_shell.py` (`ExecTool`) | **PORT (re-point)** | `claw_zero/tools/bash.py`. Same shape (timeout clamp, middle truncation, return dict) but run **locally** via `subprocess` instead of the VM RPC/MCP. |
| `tools/tools.py` (`build_tools`/`get_tool_summaries`) | **PORT (trim)** | `claw_zero/tools/registry.py`. Single tool (`bash`) only. Keep the build/summaries split so the prompt builder reads summaries uniformly. |
| `tools/tools_fs.py`, `tools_web.py`, `analyze_image.py`, `computer_handler.py`, `mcp_runtime.py`, `fs_backends.py` | **DROP** | dedicated fs tools, web, vision, computer, MCP transport — out of scope. |
| `canonical/*` | **DROP** | cua canonical bridge layer. claw-zero stays in chat shape. |
| `subagent/*` | **DROP** | delegation — deferred. |
| `adapters/*` (image retention, trajectory saver) | **DROP** | image / trajectory plumbing. |
| `deployer.py`, `transcript_to_trajectory.py` (repo root) | **DROP** | ALE deployer + trajectory export — not part of claw-zero. |

## Deferred (TODO markers, not built this milestone)

computer use, GUI, images, web search, subagent delegation, A2A network
transport, teams, cron, policy/permission gate, the `DONE` signal.
