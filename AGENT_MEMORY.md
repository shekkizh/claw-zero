# AGENT_MEMORY.md

- For the reloadable-harness work, keep the first boundary process-based and small: persist JSON state, exit with the named reload code, and let a supervisor start a fresh interpreter from the source tree. Avoid `importlib.reload()`.
- `reload_harness` should be absent unless the worker is supervisor-managed. A supervised single agent can have `reload_harness` without team tools.
- Runtime resume must append to the same session log and transcript chain. Save runtime state after writing reload audit entries so `transcript_last_entry_id` points at the latest entry.
- Source edits are shared by every agent in a worker. If a teammate improves the harness and requests reload, the restarted source applies to all agents because they share the same codebase.
- Runtime-spawned teammates must be persisted in `team_state.json`; suppress team-state writes while restoring so startup does not overwrite the saved roster before spawned agents are re-added.
- Python bytecode caches can hide very fast same-size source edits in reload smokes. Disable bytecode writes (`PYTHONDONTWRITEBYTECODE=1`) or change file size/timestamp when testing fresh-source reload behavior.
- Current defaults are intentional: OpenAI reasoning summary is `concise`, and memory-flush reserve uses `DEFAULT_MAX_OUTPUT_TOKENS + DEFAULT_TOOL_OUTPUT_TOKENS`. If tests disagree, update tests rather than changing these defaults.
