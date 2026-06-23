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
