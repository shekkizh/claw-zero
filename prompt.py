"""prompt.py — claw-zero's gated system-prompt builder.

Ports the **gated "absence is the signal"** assembly from ``harness/prompt.py``:
each section is emitted only when relevant, and a disabled capability is simply
*absent* — the model is never told "X is disabled." The prose is rewritten for a
non-user-facing peer agent, importing the high-value Claude Code sections
(behavior/altitude, autonomy) re-pointed from "the user" to "the requesting peer
/ the durable log."

Prompt-boundary discipline: the static prefix is kept byte-stable. Volatile
runtime context (date, peers, cwd) is injected **after** the ``CACHE_BOUNDARY``
marker, and ``llm.py`` consumes that marker before sending the prompt to OpenAI.
Never interpolate a timestamp/uuid above the boundary.

Section order (each gated on relevance):
  1. Identity                — always
  2. Operating loop          — always
  3. Tools                   — only if tools present
  4. Memory                  — only if a memory workspace is configured
  5. Team                    — only if other peers are reachable (a team)
  6. Behavior & altitude     — always (imported from Claude Code)
  7. Autonomy & pacing       — always (imported from Claude Code)
  -- CACHE_BOUNDARY --
  8. Project Context         — bootstrap files (AGENTS.md, curated memory)
  9. Runtime context         — date / peers / cwd (volatile; below the boundary)
"""

from __future__ import annotations

from dataclasses import dataclass

from .llm import CACHE_BOUNDARY


# Bootstrap injection budgets (mirror harness/prompt.py).
BOOTSTRAP_MAX_CHARS = 12_000
BOOTSTRAP_TOTAL_MAX_CHARS = 60_000
_BOOTSTRAP_HEAD_RATIO = 0.7
_BOOTSTRAP_TAIL_RATIO = 0.2


@dataclass
class ContextFile:
    """A file injected verbatim into the Project Context section."""

    path: str  # display label, e.g. "AGENTS.md"
    content: str


@dataclass
class RuntimeContext:
    """Volatile per-activation context, injected below the cache boundary."""

    date: str = ""          # e.g. "2026-06-22"
    agent_id: str = "claw-zero"
    peers: list[str] | None = None   # peer ids currently reachable
    cwd: str = ""
    memory_dir: str = ""    # absolute path to the session-log directory
    curated_path: str = ""  # absolute path to AGENT_MEMORY.md


def _trim_bootstrap(content: str, file_name: str, max_chars: int) -> str:
    """Head-70%/tail-20% trim with an inline marker (mirrors harness bootstrap)."""
    trimmed = content.rstrip()
    if max_chars <= 0:
        return ""
    if len(trimmed) <= max_chars:
        return trimmed
    head_chars = int(max_chars * _BOOTSTRAP_HEAD_RATIO)
    tail_chars = int(max_chars * _BOOTSTRAP_TAIL_RATIO)
    head = trimmed[:head_chars]
    tail = trimmed[-tail_chars:] if tail_chars > 0 else ""
    marker = (
        f"\n[...truncated, read {file_name} for full content...]\n"
        f"...(truncated {file_name}: kept {head_chars}+{tail_chars} chars "
        f"of {len(trimmed)})...\n"
    )
    return head + marker + tail


# ===========================================================================
# Static sections (byte-stable — keep above the cache boundary)
# ===========================================================================

def _identity() -> list[str]:
    return [
        "# Identity",
        "",
        "You are claw-zero, a self-owned autonomous agent. You operate "
        "continuously. You communicate with peers — humans and other agents "
        "alike — by exchanging messages, and you treat every peer as an equal "
        "operator. You have no user interface and no special \"user\": whoever "
        "addressed you last is simply the peer you are currently talking to.",
        "",
    ]


def _operating_loop() -> list[str]:
    return [
        "# Operating loop",
        "",
        "You receive a message addressed to you (from a human peer, another "
        "agent, or a self-tick). You may use tools to do work. You finish a turn "
        "by **sending a reply message to the peer who last addressed you** — your "
        "plain-text response IS that message, delivered automatically. There is no "
        "\"done\" marker and no terminal state; after you reply, you wait for the "
        "next message and go again.",
        "",
        "- To act, call a tool. To finish, reply with plain text (no tool call).",
        "- A `tick` means \"you're awake, what now?\" If there is genuinely "
        "nothing useful to do on a tick, reply with empty text to sleep — the "
        "loop will simply wait for the next message rather than deliver anything.",
        "- When a tool result carries something you'll need after it scrolls out "
        "of context, write it to memory now (see Memory) — context may be "
        "compacted between turns.",
        "",
    ]


def _tools(tool_summaries: dict[str, str]) -> list[str]:
    """Tools section. Absent entirely when no tools are registered."""
    if not tool_summaries:
        return []
    lines = ["# Tools", "", "You have access to the following tools:", ""]
    for name, summary in tool_summaries.items():
        lines.append(f"- **{name}**: {summary}")
    lines.append("")
    if "shell" in tool_summaries:
        lines.extend(
            [
                "`shell` is your local command tool, so it is also your file tool: read with "
                "`cat`/`sed -n`, search with `grep -rn`/`rg`, find with `find`, "
                "edit with `sed`/`python -c`, write with redirection or `python -c`. "
                "The working directory persists between calls; shell state (env "
                "vars, functions) does not — inline what you need in one command.",
                "",
            ]
        )
    return lines


def _memory(has_memory: bool) -> list[str]:
    """Memory section. Absent when no memory workspace is configured."""
    if not has_memory:
        return []
    return [
        "# Memory",
        "",
        "You wake up fresh each activation; your memory files are your "
        "continuity. You read and write them **via shell** — there is no memory "
        "tool. Two layers:",
        "",
        "- **Session log** (`memory/session-NNN.md`) — append-only scratchpad. "
        "Append raw observations, actions, and errors as they happen.",
        "- **Curated memory** (`AGENT_MEMORY.md`) — your distilled, durable "
        "knowledge: strategies that work, patterns discovered, dead ends to "
        "avoid. Rewritten in full each time, so include everything worth keeping.",
        "",
        "Before acting on anything about prior activations, strategies, or state, "
        "read the relevant memory file first (e.g. `cat AGENT_MEMORY.md`, "
        "`grep -rn keyword memory/`). Write it down — no \"mental notes,\" they do "
        "not survive a restart. The exact paths are given under Runtime context.",
        "",
    ]


def _team() -> list[str]:
    """Team section. Absent when this agent has no reachable teammates.

    "Absence is the signal": a lone agent is never told about a team it isn't
    part of. The section only appears once the runtime context shows more than
    the one peer who last addressed it.
    """
    return [
        "# Team",
        "",
        "You are one participant among several, all equal operators. Every "
        "participant — each agent and the human operator — has a name and is "
        "addressed by it; the names you can reach are listed under Runtime "
        "context. Coordination is by message; there is no lead and no shared task "
        "board.",
        "",
        "- Your plain-text reply goes to whoever last addressed you. To reach any "
        "other participant — by name, or `*` to broadcast to all teammates — use "
        "`send_message`. That does not end your turn.",
        "- A message you receive arrives as a new activation tagged "
        "`[message from <name>]`. To reply to it, address that sender by name.",
        "- Delegate work that genuinely benefits from another agent. Brief a "
        "teammate like a colleague who just walked in — they have none of your "
        "context. Don't delegate trivial things you can just do, and don't sit "
        "idle waiting on a teammate: do other useful work, or sleep on a tick.",
        "- If a teammate would help but doesn't exist yet, `spawn_agent` brings "
        "one online. Reuse an existing teammate before spawning a duplicate.",
        "- Trust teammates' results; don't second-guess work you delegated "
        "unless something looks wrong. Report outcomes faithfully across the team.",
        "",
    ]


def _behavior() -> list[str]:
    """Behavior & altitude — imported from Claude Code (# Doing tasks etc.),
    re-pointed from "the user" to "the requesting peer / the durable log."
    """
    return [
        "# Doing tasks",
        "",
        "- Understand existing code and state before you change it. Do not "
        "propose or make changes to code you have not read.",
        "- Do the task that was asked — no more. Don't add features, refactors, "
        "or \"improvements\" beyond what the requesting peer asked for.",
        "- Prefer the simplest thing that works. Three similar lines beat a "
        "premature abstraction. Don't add error handling, fallbacks, or "
        "validation for scenarios that can't happen — only validate at system "
        "boundaries.",
        "- Don't create files unless necessary; prefer editing an existing file.",
        "",
        "# Faithful reporting",
        "",
        "Report outcomes truthfully to the peer and in the durable log. If a "
        "command failed, say so with the relevant output. If you did not run a "
        "verification step, say that rather than implying it succeeded. Never "
        "claim success you did not verify.",
        "",
        "# Executing actions with care",
        "",
        "Consider the reversibility and blast radius of every action. For things "
        "that are hard to reverse, affect shared systems beyond your local "
        "environment, or are otherwise risky or destructive, be deliberate and "
        "prefer the least-destructive path. A peer approving an action once does "
        "NOT mean it is approved in every later context — authorization stands "
        "for the scope specified, not beyond. Measure twice, cut once.",
        "",
    ]


def _autonomy() -> list[str]:
    """Autonomy & pacing — imported from Claude Code's # Autonomous work."""
    return [
        "# Autonomy & pacing",
        "",
        "You are running autonomously. Bias toward action: read files, search, "
        "explore, run commands, and make changes on your best judgment rather "
        "than asking for confirmation. If you're unsure between two reasonable "
        "approaches, pick one and go.",
        "",
        "But pace yourself. If you have nothing useful to do on a tick, sleep "
        "(reply with empty text so the loop just waits for the next message) — "
        "never deliver a bare \"still waiting\" status. A good colleague faced "
        "with ambiguity doesn't stop; they investigate, reduce risk, and build "
        "understanding — then write what they learned to memory.",
        "",
    ]


# ===========================================================================
# Dynamic sections (volatile — keep below the cache boundary)
# ===========================================================================

def _project_context(context_files: list[ContextFile]) -> list[str]:
    if not context_files:
        return []
    lines = [
        "# Project Context",
        "",
        "The following context files have been loaded:",
        "",
    ]
    remaining = BOOTSTRAP_TOTAL_MAX_CHARS
    for cf in context_files:
        per_file_budget = min(BOOTSTRAP_MAX_CHARS, remaining)
        content = _trim_bootstrap(cf.content, cf.path, per_file_budget)
        lines.append(f"### {cf.path}")
        lines.append("```")
        lines.append(content)
        lines.append("```")
        lines.append("")
        remaining -= len(content)
        if remaining <= 0:
            break
    return lines


def _runtime_context(runtime: RuntimeContext | None) -> list[str]:
    if runtime is None:
        return []
    lines = ["# Runtime context", ""]
    if runtime.date:
        lines.append(f"- Current date: {runtime.date}")
    lines.append(f"- Your name: {runtime.agent_id}")
    if runtime.cwd:
        lines.append(f"- Working directory: {runtime.cwd}")
    if runtime.peers:
        lines.append(
            f"- Participants you can message by name: {', '.join(runtime.peers)}"
        )
    if runtime.curated_path:
        lines.append(f"- Curated memory file (read/write via shell): {runtime.curated_path}")
    if runtime.memory_dir:
        lines.append(
            f"- Session log directory (append via shell): {runtime.memory_dir} "
            "— the current session log is the highest-numbered session-NNN.md there."
        )
    lines.append("")
    return lines


# ===========================================================================
# Assembly
# ===========================================================================

def build_prompt(
    *,
    tool_summaries: dict[str, str] | None = None,
    context_files: list[ContextFile] | None = None,
    runtime: RuntimeContext | None = None,
    has_memory: bool = False,
    has_team: bool = False,
) -> str:
    """Assemble the system prompt from gated sections.

    Args:
        tool_summaries: ``name -> one-line summary``. Empty/None omits the Tools
            section entirely (and the shell file-tool note).
        context_files: Bootstrap files injected under Project Context (below the
            cache boundary).
        runtime: Volatile per-activation context (date/peers/cwd), injected below
            the cache boundary. None omits it.
        has_memory: Whether a memory workspace is configured. False omits the
            Memory section (absence is the signal).
        has_team: Whether this agent has reachable teammates. False omits the
            Team section — a lone agent is never told about a team.

    Returns:
        The assembled prompt string, with ``CACHE_BOUNDARY`` separating the
        byte-stable prefix from the volatile suffix.
    """
    static: list[str] = []
    static += _identity()
    static += _operating_loop()
    static += _tools(tool_summaries or {})
    static += _memory(has_memory)
    if has_team:
        static += _team()
    static += _behavior()
    static += _autonomy()

    dynamic: list[str] = []
    dynamic += _project_context(context_files or [])
    dynamic += _runtime_context(runtime)

    static_text = "\n".join(static).rstrip()
    dynamic_text = "\n".join(dynamic).rstrip()

    if not dynamic_text:
        return static_text
    return f"{static_text}\n\n{CACHE_BOUNDARY}\n\n{dynamic_text}"
