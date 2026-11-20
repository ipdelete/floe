"""`floe run`: the exit code and the stdout/stderr split a service relies on.

The command runs as a real process (no systemd) because its stream handling
works at the file-descriptor level.
"""

import json
import subprocess
import sys


def floe_run(tmp_path, source):
    script = tmp_path / "workflow.py"
    script.write_text('WORKFLOW = "nightly"\n' + source)
    return subprocess.run([sys.executable, "-m", "floe_cli", "run", str(script)],
                          capture_output=True, text=True, timeout=60)


SUCCEEDS = (
    "def fetch():\n"
    "    pass\n"
    'tasks = {"fetch": fetch}\n'
    'dag = {"fetch": set()}\n'
)

FAILS = (
    "def fetch():\n"
    '    raise RuntimeError("boom")\n'
    'tasks = {"fetch": fetch}\n'
    'dag = {"fetch": set()}\n'
)

# Writes to stdout every way a definition or task can: print at import time,
# print in a task, a raw descriptor write, and a child process.
NOISY = (
    "import os, subprocess\n"
    'print("definition says hi")\n'
    "def fetch():\n"
    '    print("task says hi")\n'
    '    os.write(1, b"raw write\\n")\n'
    '    subprocess.run(["echo", "child says hi"], check=True)\n'
    'tasks = {"fetch": fetch}\n'
    'dag = {"fetch": set()}\n'
)


def test_successful_run_exits_0(tmp_path):
    assert floe_run(tmp_path, SUCCEEDS).returncode == 0


def test_failed_run_exits_1(tmp_path):
    assert floe_run(tmp_path, FAILS).returncode == 1


def test_stdout_carries_only_events(tmp_path):
    result = floe_run(tmp_path, NOISY)

    assert [json.loads(line)["event"] for line in result.stdout.splitlines()] == [
        "run_started", "task_started", "task_completed", "run_completed"]


def test_workflow_output_goes_to_stderr(tmp_path):
    result = floe_run(tmp_path, NOISY)

    assert result.stderr.splitlines() == ["definition says hi", "task says hi", "raw write", "child says hi"]


def test_definition_error_is_printed_as_an_error_line(tmp_path):
    # History falls back to this line when a run fails before any event.
    result = floe_run(tmp_path, 'raise RuntimeError("bad definition")\n')

    assert result.stderr == f"error: {tmp_path / 'workflow.py'}: RuntimeError: bad definition\n"
