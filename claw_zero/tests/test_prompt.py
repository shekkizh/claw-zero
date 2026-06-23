"""Phase 4.1 — gated assembly ("absence is the signal") + cache boundary + snapshot."""

from claw_zero.llm import CACHE_BOUNDARY
from claw_zero.prompt import ContextFile, RuntimeContext, build_prompt


def test_no_tools_omits_tools_and_memory_sections():
    out = build_prompt(tool_summaries=None, has_memory=False)
    assert "# Tools" not in out
    assert "# Memory" not in out
    # Absence is the signal — no "disabled" wording anywhere.
    assert "disabled" not in out.lower()
    assert "not available" not in out.lower()
    # Static peer-agent sections are always present.
    assert "# Identity" in out
    assert "claw-zero" in out
    assert "# Operating loop" in out
    assert "# Autonomy & pacing" in out


def test_with_tools_and_memory_sections_appear():
    out = build_prompt(
        tool_summaries={"bash": "Run a shell command locally."},
        has_memory=True,
    )
    assert "# Tools" in out
    assert "- **bash**:" in out
    assert "# Memory" in out
    assert "AGENT_MEMORY.md" in out
    assert "session-NNN.md" in out
    # bash-is-the-file-tool note appears only when bash is present.
    assert "file tool" in out


def test_cache_boundary_separates_static_from_volatile():
    out = build_prompt(
        tool_summaries={"bash": "Run a shell command locally."},
        runtime=RuntimeContext(date="2026-06-22", agent_id="claw-zero", cwd="/tmp"),
        context_files=[ContextFile(path="AGENTS.md", content="home doc")],
        has_memory=True,
    )
    assert CACHE_BOUNDARY in out
    static, dynamic = out.split(CACHE_BOUNDARY, 1)
    # Volatile content lives BELOW the boundary (cache-safe prefix above).
    assert "2026-06-22" in dynamic
    assert "2026-06-22" not in static
    assert "# Project Context" in dynamic
    assert "# Identity" in static
    # The byte-stable prefix has no volatile values (date/cwd) interpolated.
    assert "/tmp" not in static
    assert "# Runtime context" not in static  # the section header lives below the boundary


def test_no_runtime_or_context_means_no_boundary():
    out = build_prompt(tool_summaries={"bash": "x"})
    # Nothing volatile → no boundary marker emitted, prompt is all-static.
    assert CACHE_BOUNDARY not in out


def test_snapshot_stable_prefix_is_byte_stable():
    # Two builds with different volatile context must share an identical prefix
    # (the cache key). This is the property prompt caching relies on.
    common = dict(tool_summaries={"bash": "Run a shell command locally."}, has_memory=True)
    a = build_prompt(**common, runtime=RuntimeContext(date="2026-06-22"))
    b = build_prompt(**common, runtime=RuntimeContext(date="2026-06-23"))
    prefix_a = a.split(CACHE_BOUNDARY, 1)[0]
    prefix_b = b.split(CACHE_BOUNDARY, 1)[0]
    assert prefix_a == prefix_b
