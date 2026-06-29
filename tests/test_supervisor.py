import asyncio
import os
import sys

from claw_zero import __main__ as main_mod
from claw_zero.reload_marker import SOURCE_RELOAD_MARKER
from claw_zero.runtime_state import RELOAD_REQUESTED_EXIT_CODE
from claw_zero.source_identity import collect_source_identity, format_source_identity
from claw_zero.supervisor import supervise_command


def test_worker_argv_adds_worker_and_resume_flags_once():
    argv = ["--supervise", "--model", "gpt-5.5"]
    child = main_mod._worker_argv(argv)
    assert child.count("--worker") == 1
    assert child.count("--resume-runtime-state") == 1
    assert "--model" in child

    child2 = main_mod._worker_argv([*child])
    assert child2.count("--worker") == 1
    assert child2.count("--resume-runtime-state") == 1


def test_source_identity_includes_marker_and_runtime_context(tmp_path):
    identity = collect_source_identity(
        source_root=tmp_path,
        argv=["--worker"],
        model="gpt-5.5",
        state_dir="state",
        worker=True,
    )
    assert identity["source_reload_marker"] == SOURCE_RELOAD_MARKER
    assert identity["argv"] == ["--worker"]
    assert identity["worker"] is True
    formatted = format_source_identity(identity)
    assert "marker=claw-zero-source-marker-v1" in formatted
    assert "state=state" in formatted


def test_supervisor_marker_reload_smoke(tmp_path):
    marker = tmp_path / "marker.py"
    worker = tmp_path / "worker.py"
    log = tmp_path / "seen.txt"
    count = tmp_path / "count.txt"
    marker.write_text("MARKER = 'before'\n", encoding="utf-8")
    worker.write_text(
        """
import pathlib
import sys
from marker import MARKER

root = pathlib.Path(__file__).parent
log = root / "seen.txt"
count = root / "count.txt"
log.write_text((log.read_text() if log.exists() else "") + MARKER + "\\n")
if not count.exists():
    (root / "marker.py").write_text("MARKER = 'after'\\n")
    count.write_text("1")
    raise SystemExit(75)
raise SystemExit(0)
""".lstrip(),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    rc = asyncio.run(
        supervise_command(
            [sys.executable, str(worker)],
            cwd=tmp_path,
            env=env,
            max_reloads=2,
        )
    )

    assert rc == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["before", "after"]


def test_supervisor_caps_reload_loop(tmp_path):
    worker = tmp_path / "worker.py"
    log = tmp_path / "starts.txt"
    worker.write_text(
        """
import pathlib
root = pathlib.Path(__file__).parent
log = root / "starts.txt"
log.write_text((log.read_text() if log.exists() else "") + "start\\n")
raise SystemExit(75)
""".lstrip(),
        encoding="utf-8",
    )

    rc = asyncio.run(
        supervise_command(
            [sys.executable, str(worker)],
            cwd=tmp_path,
            env=os.environ,
            max_reloads=1,
        )
    )

    assert rc == RELOAD_REQUESTED_EXIT_CODE
    assert log.read_text(encoding="utf-8").splitlines() == ["start", "start"]


def test_supervisor_toy_tool_reload_smoke(tmp_path):
    tool = tmp_path / "toy_tool.py"
    worker = tmp_path / "worker.py"
    log = tmp_path / "tool_seen.txt"
    count = tmp_path / "count.txt"
    tool.write_text("def run():\n    return 'old-tool'\n", encoding="utf-8")
    worker.write_text(
        """
import pathlib
from toy_tool import run

root = pathlib.Path(__file__).parent
log = root / "tool_seen.txt"
count = root / "count.txt"
log.write_text((log.read_text() if log.exists() else "") + run() + "\\n")
if not count.exists():
    (root / "toy_tool.py").write_text("def run():\\n    return 'new-tool'\\n")
    count.write_text("1")
    raise SystemExit(75)
raise SystemExit(0)
""".lstrip(),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    rc = asyncio.run(
        supervise_command(
            [sys.executable, str(worker)],
            cwd=tmp_path,
            env=env,
            max_reloads=2,
        )
    )

    assert rc == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["old-tool", "new-tool"]


def test_supervisor_state_continuity_smoke(tmp_path):
    worker = tmp_path / "worker.py"
    state = tmp_path / "state.json"
    log = tmp_path / "state_seen.txt"
    worker.write_text(
        """
import json
import pathlib

root = pathlib.Path(__file__).parent
state = root / "state.json"
log = root / "state_seen.txt"
if not state.exists():
    state.write_text(json.dumps({"fact": "alpha", "turns": ["before-reload"]}))
    raise SystemExit(75)
payload = json.loads(state.read_text())
log.write_text(payload["fact"] + ":" + payload["turns"][0] + "\\n")
raise SystemExit(0)
""".lstrip(),
        encoding="utf-8",
    )

    rc = asyncio.run(
        supervise_command(
            [sys.executable, str(worker)],
            cwd=tmp_path,
            env=os.environ,
            max_reloads=2,
        )
    )

    assert rc == 0
    assert log.read_text(encoding="utf-8") == "alpha:before-reload\n"
