"""Contract between journald and the history reader.

C6  journald -> history:  records a service run wrote come back as that run's tasks.
"""

import subprocess
import sys
import uuid

import pytest

import floe

pytestmark = pytest.mark.live


@pytest.mark.parametrize("fails, expected_status", [(False, "succeeded"), (True, "failed")])
def test_history_reads_back_the_run_and_its_tasks(tmp_path, fails, expected_status):
    # A unique name per run, so leftover journal history can't satisfy the assert.
    name = f"floe-contract-{uuid.uuid4().hex[:12]}"
    script = tmp_path / "workflow.py"
    body = '    raise RuntimeError("boom")\n' if fails else '    return 42\n'
    script.write_text(
        f"WORKFLOW = {name!r}\n"
        "def work():\n"
        f"{body}"
        'tasks = {"work": work}\n'
        'dag = {"work": set()}\n'
    )
    subprocess.run(
        ["systemd-run", "--user", f"--unit={name}", "--wait", "--collect",
         sys.executable, "-m", "floe_cli", "run", str(script)],
        capture_output=True, timeout=60,
    )

    runs = floe.list_runs(name, floe.Journal(), floe.Systemd())

    assert runs[0].status == expected_status
    # Tasks come only from the run's events; systemd's own records can't supply them.
    assert [(task.name, task.status) for task in runs[0].tasks] == [("work", expected_status)]
