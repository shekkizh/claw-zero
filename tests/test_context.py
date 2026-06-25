"""Phase 6 — token estimation, compaction (budget + summary + pairing), transcript."""

import asyncio
import json

from claw_zero import llm
from claw_zero.context import token_estimation as te
from claw_zero.context.compaction import (
    CompactionResult,
    compact_messages,
    repair_tool_use_result_pairing,
    split_preserved_recent_turns,
)
from claw_zero.context.transcript import Transcript


# --- token estimation -------------------------------------------------------

def test_estimate_positive_int():
    msgs = [
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "a longer reply with several words in it"},
    ]
    assert te.estimate_messages_tokens(msgs) > 0
    assert isinstance(te.estimate_message_tokens(msgs[0]), int)


# --- tool pairing repair ----------------------------------------------------

def test_repair_drops_orphan_result():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "ghost", "content": "orphaned"},
    ]
    rep = repair_tool_use_result_pairing(msgs)
    assert rep.dropped_orphan_count == 1
    assert all(m.get("role") != "tool" for m in rep.messages)


def test_repair_inserts_synthetic_for_unmatched_call():
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "send_message", "arguments": "{}"}}
        ]},
    ]
    rep = repair_tool_use_result_pairing(msgs)
    assert rep.added_synthetic_count == 1
    tool_msgs = [m for m in rep.messages if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_1"


def test_repair_keeps_valid_pair_intact():
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "send_message", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    rep = repair_tool_use_result_pairing(msgs)
    assert rep.dropped_orphan_count == 0
    assert rep.added_synthetic_count == 0
    assert len(rep.messages) == 2


def test_split_preserves_recent_turns():
    msgs = []
    for i in range(6):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    pruneable, preserved = split_preserved_recent_turns(msgs, preserve_count=2)
    # 2 assistant turns preserved → last 2 assistant msgs (+ trailing) kept.
    assert any(m["content"] == "a5" for m in preserved)
    assert any(m["content"] == "a4" for m in preserved)
    assert all(m["content"] != "a5" for m in pruneable)


# --- compaction (mocked summarizer) ----------------------------------------

def _make_over_budget_history(n_turns: int):
    """Build a long history with intact tool_call/result pairs and big payloads."""
    msgs = [{"role": "user", "content": "Kick off a long task. " + "x" * 500}]
    for i in range(n_turns):
        cid = f"call_{i}"
        msgs.append({
            "role": "assistant",
            "content": f"step {i}",
            "tool_calls": [{
                "id": cid,
                "function": {
                    "name": "send_message",
                    "arguments": json.dumps({"to": "operator", "content": f"step {i}"}),
                },
            }],
        })
        msgs.append({"role": "tool", "tool_call_id": cid, "content": ("result " + "y" * 800)})
    return msgs


def test_compact_fits_budget_keeps_summary_and_pairing(monkeypatch):
    async def fake_call(model, messages, **kwargs):
        return llm.LLMResult(text="## Goal\nDo the long task.\n## Next Steps\n1. continue", tool_calls=[])

    monkeypatch.setattr(llm, "call", fake_call)

    history = _make_over_budget_history(40)
    # Small window so the history is comfortably over budget.
    window = 4000
    before = te.estimate_messages_tokens(history)

    result: CompactionResult = asyncio.run(
        compact_messages(history, "gpt-5.5", context_window=window, recent_turns_preserve=3)
    )

    assert isinstance(result, CompactionResult)
    assert result.tokens_before == before
    # Output is smaller than the input.
    assert result.tokens_after < result.tokens_before
    # Summary text is present (came from the mocked summarizer).
    assert "Goal" in result.summary

    # Rebuild the post-compaction list the way the loop will, and verify pairing.
    kept = history[result.first_kept_message_index:]
    rebuilt = [{"role": "user", "content": f"[Earlier context summary]\n{result.summary}"}] + kept
    rep = repair_tool_use_result_pairing(rebuilt)
    # No orphans/dups remain, and every tool result has a matching assistant call.
    assert rep.dropped_orphan_count == 0
    assert rep.dropped_duplicate_count == 0
    call_ids = {tc["id"] for m in rep.messages if m.get("role") == "assistant" for tc in m.get("tool_calls", [])}
    for m in rep.messages:
        if m.get("role") == "tool":
            assert m["tool_call_id"] in call_ids


def test_compact_empty_history():
    result = asyncio.run(compact_messages([], "gpt-5.5", context_window=10000))
    assert result.tokens_before == 0
    assert result.first_kept_message_index == 0


# --- transcript -------------------------------------------------------------

def test_transcript_writes_readable_jsonl(tmp_path):
    t = Transcript(agent_id="claw-zero", base_dir=tmp_path)
    run = t.init_session(model="gpt-5.5")
    assert run == 1
    mid = t.append_message("user", "hello")
    t.append_message("assistant", "hi", usage={"input_tokens": 10, "output_tokens": 3}, stop_reason="stop")
    t.append_compaction("a summary", first_kept_entry_id=mid, tokens_before=1234)

    lines = [json.loads(l) for l in t.path.read_text().splitlines() if l.strip()]
    types = [e["type"] for e in lines]
    assert types == ["session", "message", "message", "compaction"]
    # parentId chain links entries.
    assert lines[1]["parentId"] == lines[0]["id"]
    assert lines[2]["message"]["usage"]["input_tokens"] == 10
    assert lines[3]["summary"] == "a summary"
    assert t.transcript_bytes > 0


def test_transcript_records_tool_call_metadata(tmp_path):
    t = Transcript(agent_id="claw-zero", base_dir=tmp_path)
    t.init_session(model="gpt-5.5")
    t.append_message(
        "assistant",
        "",
        stop_reason="tool_calls",
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": "send_message", "arguments": '{"to": "coder"}'},
        }],
    )
    t.append_message("tool", '{"success": true}', tool_call_id="call_1")

    lines = [json.loads(l) for l in t.path.read_text().splitlines() if l.strip()]
    assistant = lines[1]["message"]
    tool = lines[2]["message"]
    assert assistant["stopReason"] == "tool_calls"
    assert assistant["toolCalls"][0]["function"]["name"] == "send_message"
    assert tool["toolCallId"] == "call_1"


def test_transcript_records_hosted_tool_result_metadata(tmp_path):
    t = Transcript(agent_id="claw-zero", base_dir=tmp_path)
    t.init_session(model="gpt-5.5")
    t.append_message(
        "assistant",
        "Current result.",
        stop_reason="completed",
        tool_calls=[{
            "id": "ws_1",
            "type": "web_search",
            "status": "completed",
            "action": {"type": "search", "query": "latest AI news"},
        }],
        tool_results=[{
            "type": "web_search_result",
            "toolCallId": "ws_1",
            "content": [{
                "type": "output_text",
                "text": "Current result.",
                "annotations": [{"type": "url_citation", "url": "https://example.com", "title": "Example"}],
            }],
        }],
    )

    lines = [json.loads(l) for l in t.path.read_text().splitlines() if l.strip()]
    assistant = lines[1]["message"]
    assert assistant["toolCalls"][0]["type"] == "web_search"
    assert assistant["toolCalls"][0]["action"]["query"] == "latest AI news"
    assert assistant["toolResults"][0]["toolCallId"] == "ws_1"
    assert assistant["toolResults"][0]["content"][0]["annotations"][0]["url"] == "https://example.com"


def test_transcript_records_reasoning_summary_metadata(tmp_path):
    t = Transcript(agent_id="claw-zero", base_dir=tmp_path)
    t.init_session(model="gpt-5.5")
    t.append_message(
        "assistant",
        "Done.",
        stop_reason="completed",
        reasoning_summaries=[{
            "id": "rs_1",
            "summary": [{
                "type": "summary_text",
                "text": "Checked the current state and chose the direct fix.",
            }],
        }],
    )

    lines = [json.loads(l) for l in t.path.read_text().splitlines() if l.strip()]
    assistant = lines[1]["message"]
    assert assistant["reasoningSummaries"][0]["id"] == "rs_1"
    assert assistant["reasoningSummaries"][0]["summary"][0]["type"] == "summary_text"
    assert "direct fix" in assistant["reasoningSummaries"][0]["summary"][0]["text"]


def test_transcript_run_number_increments(tmp_path):
    t = Transcript(base_dir=tmp_path)
    assert t.init_session() == 1
    t2 = Transcript(base_dir=tmp_path)
    assert t2.init_session() == 2
