"""Live: deploy a workflow with the floe CLI, run it under user systemd, and
read its outcome back from journald history."""

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

FLOE = str(Path(sys.executable).parent / "floe")


def floe(*args, check=True):
    return subprocess.run([FLOE, *args], check=check, capture_output=True, text=True, timeout=120)


def test_failed_run_is_reported_as_failed(tmp_path):
    # A unique name per run, so leftover journal history can't satisfy the assert.
    name = f"floe-e2e-{uuid.uuid4().hex[:12]}"
    workflow = tmp_path / "workflow.py"
    workflow.write_text(
        f"WORKFLOW = {name!r}\n"
        'SCHEDULE = ["2099-01-01"]\n'
        "def boom():\n"
        '    raise RuntimeError("boom")\n'
        'tasks = {"boom": boom}\n'
        'dag = {"boom": set()}\n'
    )

    floe("install", str(workflow))
    try:
        floe("start", str(workflow), check=False)  # exits nonzero because the run fails
        runs = json.loads(floe("runs", str(workflow), "--json", "-n", "1").stdout)
    finally:
        floe("uninstall", str(workflow), check=False)

    assert runs[0]["status"] == "failed"
