"""Contract between journald and the history reader.

C6  journald -> history:  records a service run wrote come back as that run's tasks.
"""

import subprocess
import sys
import uuid

import pytest

import floe

pytestmark = pytest.mark.live


def test_history_reads_back_the_runs_tasks(tmp_path):
    # A unique name per run, so leftover journal history can't satisfy the assert.
    name = f"floe-contract-{uuid.uuid4().hex[:12]}"
    script = tmp_path / "workflow.py"
    script.write_text(
        f"WORKFLOW = {name!r}\n"
        "def boom():\n"
        '    raise RuntimeError("boom")\n'
        'tasks = {"boom": boom}\n'
        'dag = {"boom": set()}\n'
    )
    subprocess.run(
        ["systemd-run", "--user", f"--unit={name}", "--wait", "--collect",
         sys.executable, "-m", "floe_cli", "run", str(script)],
        capture_output=True, timeout=60,
    )

    runs = floe.list_runs(name, floe.Journal(), floe.Systemd())

    # Tasks come only from the run's events; systemd's own records can't supply them.
    assert [(task.name, task.status) for task in runs[0].tasks] == [("boom", "failed")]
