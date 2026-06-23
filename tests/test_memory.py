"""Phase 5 — MemoryStore CRUD + traversal guard; flush trigger + memory_write routing."""

import asyncio

import pytest

from claw_zero import llm
from claw_zero.memory.flush import (
    FlushState,
    maybe_flush_memory,
    run_memory_flush,
    should_run_memory_flush,
)
from claw_zero.memory.store import MemoryStore


def test_store_session_append_and_curated_roundtrip(tmp_path):
    store = MemoryStore(agent_id="claw-zero", base_dir=tmp_path)
    rel = store.init_session()
    assert rel.endswith("session-001.md")

    store.append_session("first observation")
    store.append_session("second observation")

    store.write_curated("# Curated\n\nstrategy A works.")
    assert "strategy A works" in store.read_curated()

    # Read the session log back via read_file and confirm both entries are there.
    log = store.read_file(rel)
    assert "first observation" in log
    assert "second observation" in log


def test_store_session_numbering_increments(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    a = store.init_session()
    b = store.init_session()
    assert a.endswith("session-001.md")
    assert b.endswith("session-002.md")
    assert sorted(store.list_session_files())[-1].endswith("session-002.md")


def test_curated_overwrite_replaces_full_file(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    store.write_curated("v1")
    store.write_curated("v2")
    assert store.read_curated() == "v2"


def test_append_without_init_raises(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    with pytest.raises(RuntimeError):
        store.append_session("nope")


def test_read_file_traversal_guard(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    with pytest.raises(ValueError):
        store.read_file("../../etc/passwd")
    # A sibling whose string starts with base_dir must also be rejected.
    with pytest.raises(ValueError):
        store.read_file("../" + tmp_path.name + "-evil/secret.md")


def test_flush_trigger_thresholds():
    state = FlushState()
    # 200K window @ 0.80 → trigger 160K; threshold 160K-20K-4K = 136K.
    assert should_run_memory_flush(state, current_tokens=135_000, context_window=200_000) is False
    assert should_run_memory_flush(state, current_tokens=137_000, context_window=200_000) is True
    # Transcript-size trigger fires independently of token count.
    assert should_run_memory_flush(
        state, current_tokens=0, context_window=200_000, transcript_bytes=3 * 1024 * 1024
    ) is True


def test_flush_dedup_within_compaction_cycle():
    state = FlushState(compaction_count=0)
    assert should_run_memory_flush(state, current_tokens=137_000, context_window=200_000) is True
    state.flushed_at_compaction_count = 0  # simulate a flush having run
    assert should_run_memory_flush(state, current_tokens=137_000, context_window=200_000) is False
    state.compaction_count = 1  # new compaction cycle → eligible again
    assert should_run_memory_flush(state, current_tokens=137_000, context_window=200_000) is True


def test_run_flush_routes_memory_write(tmp_path, monkeypatch):
    store = MemoryStore(base_dir=tmp_path)
    store.init_session()
    state = FlushState()

    async def fake_call(model, messages, **kwargs):
        return llm.LLMResult(
            text="",
            tool_calls=[
                llm.ToolCall(id="c1", name="memory_write",
                             arguments='{"content": "remember: build is green", "target": "session"}'),
                llm.ToolCall(id="c2", name="memory_write",
                             arguments='{"content": "# Strategy\\nuse bash for files", "target": "curated"}'),
            ],
            finish_reason="tool_calls",
        )

    monkeypatch.setattr(llm, "call", fake_call)

    ran = asyncio.run(run_memory_flush(
        model="openai/gpt-5.5", messages=[{"role": "user", "content": "hi"}],
        memory_store=store, state=state,
    ))
    assert ran is True
    # session log gained the observation; curated gained the strategy.
    assert "build is green" in store.read_file(store.list_session_files()[0])
    assert "use bash for files" in store.read_curated()
    # dedup recorded.
    assert state.flushed_at_compaction_count == state.compaction_count


def test_maybe_flush_respects_trigger(tmp_path, monkeypatch):
    store = MemoryStore(base_dir=tmp_path)
    store.init_session()
    state = FlushState()
    calls = {"n": 0}

    async def fake_call(model, messages, **kwargs):
        calls["n"] += 1
        return llm.LLMResult(text="[!silent]", tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(llm, "call", fake_call)

    # Below threshold → no flush, no call.
    ran = asyncio.run(maybe_flush_memory(
        model="m", messages=[], memory_store=store, state=state,
        current_tokens=1000, context_window=200_000,
    ))
    assert ran is False and calls["n"] == 0

    # Above threshold → flush runs.
    ran = asyncio.run(maybe_flush_memory(
        model="m", messages=[], memory_store=store, state=state,
        current_tokens=150_000, context_window=200_000,
    ))
    assert ran is True and calls["n"] == 1
