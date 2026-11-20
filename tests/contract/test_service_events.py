"""Contracts between `floe run` as a systemd service and the history reader.

C3  floe_cli -> history:   stdout carries only event JSON lines, even when tasks print.
C4  systemd -> runner:     every event's run_id is the service's invocation ID,
                           which is what history matches journal records on.
"""

import json
import re
import subprocess
import sys

import pytest

import floe

pytestmark = pytest.mark.live

WORKFLOW = (
    'WORKFLOW = "floe-contract-events"\n'
    "def boom():\n"
    '    print("task output on stdout")\n'
    '    raise RuntimeError("boom")\n'
    'tasks = {"boom": boom}\n'
    'dag = {"boom": set()}\n'
)


@pytest.fixture
def service_run(tmp_path):
    """Run the workflow as a transient user service, piping its stdout back."""
    script = tmp_path / "workflow.py"
    script.write_text(WORKFLOW)
    return subprocess.run(
        ["systemd-run", "--user", "--pipe", "--wait", "--collect",
         sys.executable, "-m", "floe_cli", "run", str(script)],
        capture_output=True, text=True, timeout=60,
    )


def test_stdout_carries_only_events(service_run):
    events = [floe.Event.from_dict(json.loads(line)) for line in service_run.stdout.splitlines()]

    assert [event.event for event in events] == ["run_started", "task_started", "task_completed", "run_completed"]


def test_run_id_is_the_invocation_id(service_run):
    invocation = re.search(r"invocation ID: ([0-9a-f]{32})", service_run.stderr).group(1)

    assert {json.loads(line)["run_id"] for line in service_run.stdout.splitlines()} == {invocation}
