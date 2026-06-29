"""Phase 3 acceptance — local shell runs, times out, persists cwd; registry split."""

import asyncio
import os

from claw_zero.tools.bash import BashTool
from claw_zero.tools.registry import build_tools, get_tool_summaries


def test_echo_and_pwd_exit_zero():
    async def run():
        tool = BashTool()
        return await tool.run({"command": "echo hi && pwd"})

    res = asyncio.run(run())
    assert res["success"] is True
    assert res["exit_code"] == 0
    assert "hi" in res["stdout"]


def test_nonzero_exit_code_surfaced():
    async def run():
        tool = BashTool()
        return await tool.run({"command": "exit 7"})

    res = asyncio.run(run())
    assert res["success"] is True
    assert res["exit_code"] == 7
    assert res["status"] == "failed"


def test_timeout_does_not_hang():
    async def run():
        tool = BashTool()
        return await tool.run({"command": "sleep 999", "timeout": 1})

    res = asyncio.run(run())
    assert res["success"] is False
    assert res.get("timed_out") is True
    assert "timed out" in res["error"]


def test_cwd_persists_between_calls(tmp_path):
    sub = tmp_path / "subdir"
    sub.mkdir()

    async def run():
        tool = BashTool(cwd=str(tmp_path))
        await tool.run({"command": "cd subdir"})
        # Next call should start inside subdir.
        return tool.cwd, await tool.run({"command": "pwd"})

    cwd_after, res = asyncio.run(run())
    assert os.path.realpath(cwd_after) == os.path.realpath(str(sub))
    assert os.path.realpath(res["stdout"].strip()) == os.path.realpath(str(sub))


def test_shell_state_does_not_persist():
    async def run():
        tool = BashTool()
        await tool.run({"command": "export FOO=bar"})
        return await tool.run({"command": "echo \"[${FOO:-unset}]\""})

    res = asyncio.run(run())
    assert "[unset]" in res["stdout"]


def test_output_truncation_keeps_head_and_tail():
    async def run():
        tool = BashTool(max_output_chars=200)
        # Print 1000 distinct lines; head and tail must both survive.
        return await tool.run({"command": "for i in $(seq 1 1000); do echo line_$i; done"})

    res = asyncio.run(run())
    assert res["truncated"] is True
    assert "line_1" in res["stdout"]
    assert "line_1000" in res["stdout"]
    assert "truncated" in res["stdout"]


def test_shell_call_output_shape():
    async def run():
        tool = BashTool()
        return await tool.run_shell_call({
            "call_id": "sh_1",
            "commands": ["echo hi"],
            "timeout_ms": 120000,
            "max_output_length": 4096,
        })

    res = asyncio.run(run())
    assert res["type"] == "shell_call_output"
    assert res["call_id"] == "sh_1"
    assert res["max_output_length"] == 4096
    assert res["output"][0]["stdout"] == "hi\n"
    assert res["output"][0]["stderr"] == ""
    assert res["output"][0]["outcome"] == {"type": "exit", "exit_code": 0}


def test_registry_local_shell_split():
    reg = build_tools(BashTool())
    assert [spec["type"] for spec in reg.specs] == ["shell"]
    assert reg.specs[0]["environment"] == {"type": "local"}
    assert set(reg.handlers) == set()
    assert callable(reg.shell_handler)
    assert reg.shell_tool is not None
    summaries = get_tool_summaries(reg)
    assert set(summaries) == {"shell"}
    assert summaries["shell"]  # non-empty one-liner
