# Session 001 — claw-zero build log

This is the build's own dog-food log (the brief: "you are building the thing that
needs memory; eat your own dog food"). Runtime state logs live separately under
the gitignored `claw_zero_state/<agent_id>/memory/`.

## Shipped (all 11 phases, 50 tests green)

- 2026-06-22: Phases 0–10 complete. `python -m claw_zero` runs a self-owned loop
  that never exits on its own; a human peer over stdio converses with it; the
  agent uses exactly `bash` (client-side); an activation ends by **delivering a
  message**, never `DONE`. Durable memory (session log + curated `AGENT_MEMORY.md`)
  and flush-before-compaction work; in-place compaction repairs tool pairing. No
  `cua-*` dependency, no computer-use/GUI/image/subagent code. A2A transport,
  teams, cron, policy gate are absent by design (TODO markers only).
- Smoke test passed live against `openai/gpt-5.5`: "What files are in the current
  directory? Then save a note that you checked." → bash `ls`, a memory write via
  bash to the real `session-001.md`, and a plain-text reply listing files. A
  two-message run confirmed the loop stays alive for follow-ups.

## Surprises / decisions worth keeping

- **Self-contained port, not import.** `harness/__init__.py` eagerly imports the
  cua SDK (`import agent`), so `claw_zero` could not `import harness.*` without
  re-introducing the dependency we were dropping. Everything kept was ported into
  `claw_zero/` instead. See `PORTING.md`.
- **Chat shape, not canonical blocks.** The harness loop speaks CUA canonical
  message blocks; claw-zero's loop speaks OpenAI chat shape (`assistant.tool_calls`
  + `role:"tool"`/`tool_call_id`). Compaction's tool-pairing repair and the
  flush/summary serializers were re-pointed to chat shape.
- **Legacy root package removed.** The repo-root `__init__.py`/`config.py`/
  `deployer.py` (the old ALE Claw deployer) made pytest treat the whole repo as a
  package and pulled cua via `deployer.py`. Moved to `legacy/` (preserved, not a
  package).
- **bash process-group kill.** Killing just the `/bin/sh` on timeout orphaned its
  `sleep` child; fixed with `start_new_session=True` + `os.killpg` so the whole
  tree dies and the "command is killed" promise is honest.
- **Memory paths must be surfaced.** First smoke run, the agent *claimed* it saved
  a note but wrote `./memory/session-001.md` relative to cwd, not the store's real
  file — a faithful-reporting hazard. Fixed by surfacing the absolute memory
  paths in the prompt's Runtime context (below the cache boundary).
- **Graceful shutdown.** Cancelling the outer loop on stdin EOF killed an
  in-flight `llm.call` ("coroutine never awaited"). `__main__` now drains the
  mailbox and waits for the loop to be idle before tearing down; the outer loop
  itself still never returns on its own.

## 2026-06-25 OpenAI-only LLM migration

- Replaced the LiteLLM adapter path in `llm.py` with direct
  `AsyncOpenAI().responses.create` usage. First pass mistakenly targeted Chat
  Completions; corrected to the Responses API after checking official docs.
  The harness-facing `llm.call` contract stays the same (`LLMResult`,
  chat-shaped internal messages, OpenAI tool specs), while the adapter converts
  to Responses `input`, `instructions`, `max_output_tokens`, `reasoning`, and
  flat function tools.
- Changed the default model from `openai/gpt-5.5` to native OpenAI id
  `gpt-5.5`. The old `openai/<id>` prefix is still accepted and stripped for
  compatibility; other provider prefixes now raise `ValueError`.
- `CACHE_BOUNDARY` remains in prompt assembly for byte-stable prefix separation,
  but `llm.apply_cache_markers` now only strips provider cache metadata and
  consumes the boundary before sending OpenAI requests.
- Swapped runtime dependency from `litellm>=1.80` to `openai>=2.43` and
  regenerated `uv.lock` with `uv lock --offline`; uv needed escalated access to
  read the user-level package cache.
- Verification: `.venv/bin/pytest` passed (69 tests), and
  `.venv/bin/python -m claw_zero --help` shows `--model` as an OpenAI model id
  with default `gpt-5.5`. Active code/docs/package files no longer reference
  LiteLLM; remaining mentions are historical source-analysis docs only.
- Post-sync verification: `uv sync --offline --all-extras` removed the stale
  LiteLLM install from `.venv`; `.venv/bin/python` reports
  `litellm_installed=False` and `openai_installed=True`. The installed OpenAI SDK
  is `2.43.0`; offline inspection confirms `AsyncOpenAI(...).responses.create`
  exists and accepts `input`, `instructions`, `max_output_tokens`, `reasoning`,
  `tools`, `temperature`, and `timeout`. Reran `.venv/bin/pytest`: 69 passed.

## 2026-06-25 LLM adapter simplification

- Simplified `llm.py` after the OpenAI-only migration: removed the old
  provider-style `ThinkLevel` enum, `MAX_EFFORT`, `resolve_model`, and
  thinking-parameter helpers. `llm.call` now always sends the fixed Responses
  API `reasoning={"effort": "xhigh"}` block.
- Moved tool specs to Responses-native flat function definitions at the source
  (`tools/registry.py` and the memory flush tool), so `llm.py` no longer converts
  Chat Completions-style nested `function` specs.
- `LLMResult` now keeps `response_items` from OpenAI output; `inner_loop.py`
  stores them on assistant turns so later tool-result requests can replay the
  model output items directly.
- Verification: `.venv/bin/pytest` passed (66 tests).

## 2026-06-25 Hosted web search

- Checked official OpenAI docs: new Responses integrations should use the hosted
  `{"type": "web_search"}` tool, not the legacy `web_search_preview` tool.
- Added OpenAI hosted `web_search` to the baseline agent tool specs with no
  local handler. Local function handlers remain only for tools claw-zero executes
  itself, such as `bash`, `send_message`, and `spawn_agent`.
- Updated `llm.py` normalization to append URL citation annotations from
  web-search-backed `output_text` parts into a visible `Sources:` section in the
  plain-text reply. The raw Responses output items are still stored for replay.
- Verification: `.venv/bin/pytest` passed (67 tests).

## 2026-06-25 Local Shell tool clarification

- Checked official OpenAI Shell docs again for the local-runtime path. Local
  Shell is explicitly supported through Responses with
  `{"type": "shell", "environment": {"type": "local"}}`; the integration must
  execute returned `shell_call` actions locally and send `shell_call_output`
  items back on the next request.
- Recommendation: local Shell is a reasonable replacement target for the custom
  `bash` function schema, but it is still an implementation migration rather
  than a zero-risk toggle. The executor/sandbox can be reused; the loop must
  learn native `shell_call` / `shell_call_output` items and preserve timeout,
  output, non-interactive, and audit behavior.

## 2026-06-25 Local Shell tool migration

- Migrated the active command surface from a custom `bash` function tool to
  OpenAI's native local Shell tool spec:
  `{"type": "shell", "environment": {"type": "local"}}`. `tools/bash.py` remains
  the local subprocess executor, but `tools/registry.py` no longer emits it as a
  function spec; it stores `shell_handler` / `shell_tool` separately.
- Extended `llm.py` with `ShellCall` normalization and replay support for
  Responses-native `shell_call` / `shell_call_output` items. Function calls
  remain supported for `send_message`, `spawn_agent`, and `memory_write`.
- Updated `inner_loop.py` to dispatch native Shell calls through the local
  executor and append `shell_call_output` items directly into conversation
  history. Function-tool dispatch remains unchanged.
- Updated prompts, README/status docs, and tests so the model-facing tool is
  `shell`; `bash` remains only an internal executor/file name or historical
  source-analysis term.
- Verification: focused shell/LLM/prompt/team tests passed (26 tests), full
  `.venv/bin/pytest` passed (68 tests), and `git diff --check` was clean.

## 2026-06-25 Transcript tool-call persistence

- Operator observed that `claw_zero_state/*/transcript.jsonl` showed assistant
  turns ending with `stopReason: "tool_calls"` but empty content and no saved
  tool-call request, followed only by result entries.
- Added optional transcript metadata fields: assistant message entries can now
  include `toolCalls`, and tool/shell result entries can include `toolCallId`.
- Wired `inner_loop.py` to record normalized function calls plus native Shell
  calls in assistant transcript entries, and to tag tool/shell observations with
  the matching call id.
- Added regression tests in `tests/test_context.py` and `tests/test_inner_loop.py`.
  Verification: focused transcript/inner-loop tests passed (13 tests), full
  `.venv/bin/pytest` passed (69 tests), and `git diff --check` passed for the
  touched files.

## 2026-06-25 Transcript hosted search persistence

- Follow-up: hosted OpenAI `web_search` calls/results also needed transcript
  persistence. Official docs say Responses web search output includes a
  `web_search_call` item plus a `message` item with text and URL-citation
  annotations.
- Extended transcript metadata with optional `toolResults`.
- Wired `inner_loop.py` so assistant transcript entries now include hosted
  web-search calls in `message.toolCalls` and citation-bearing hosted-search
  output in `message.toolResults`. This is on the assistant entry because
  hosted search returns in the same Responses call, not as a separate local
  tool-result turn.
- Hardened `llm._dump_item` to preserve `web_search_call` id/status/action for
  SDK-like objects without `model_dump()`.
- Added regression tests in `tests/test_context.py`, `tests/test_inner_loop.py`,
  and `tests/test_llm.py`. Verification: focused tests passed (22 tests), full
  `.venv/bin/pytest` passed (72 tests), and `git diff --check` passed for the
  touched files.
