"""Opt-in test of the actual workflow and user systemd session.

This deploys disposable workflow units, executes the tasks, and always attempts
to uninstall the units afterward. Normal pytest runs skip this module's test.
"""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from floe.deploy import Deployment, user_unit_dir
from floe.store import UnitStore
from floe.systemd import Systemd

pytestmark = pytest.mark.systemd_e2e
ROOT = Path(__file__).parents[1]


def test_clean_redeploy_run_and_journal():
    script = ROOT / "workflow.py"
    config = runpy.run_path(str(script))
    dep = Deployment(config["WORKFLOW"], script, config["SCHEDULE"])
    store = UnitStore(user_unit_dir())
    sd = Systemd()

    def command(*args):
        result = subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    # Refuse foreign/masked units before taking any action. These units are
    # disposable test infrastructure; do not back them up or restore them.
    expected = dep.render_units()
    exec_start = next(line for line in expected[dep.service_name].splitlines() if line.startswith("ExecStart="))
    for name in expected:
        store.preflight(name)
        file = store.path / name
        if file.exists():
            text = file.read_text()
            if name == dep.service_name:
                assert exec_start in text.splitlines(), "existing service runs a different script"

    def assert_clean():
        for name in expected:
            assert not (store.path / name).exists(), f"test left {name} installed"
            assert sd.show(name, "LoadState")["LoadState"] == "not-found"
            for suffix in ("wants", "requires", "upholds"):
                for directory in store.path.glob(f"*.{suffix}"):
                    assert not (directory / name).is_symlink()

    try:
        command("uninstall")
        assert_clean()

        command("install")
        for name, text in expected.items():
            assert (store.path / name).read_text() == text
        timer = sd.show(dep.timer_name, "ActiveState", "UnitFileState")
        assert timer == {"ActiveState": "active", "UnitFileState": "enabled"}
        assert (store.path / "timers.target.wants" / dep.timer_name).resolve() == store.path / dep.timer_name

        mtimes = {name: (store.path / name).stat().st_mtime_ns for name in expected}
        assert "nothing (up to date)" in command("install")
        assert mtimes == {name: (store.path / name).stat().st_mtime_ns for name in expected}

        command("start")
        service = sd.show(dep.service_name, "Result", "ExecMainStatus", "InvocationID")
        assert service["Result"] == "success"
        assert service["ExecMainStatus"] == "0"
        invocation = service["InvocationID"]
        assert invocation
        # Synchronize pending journal writes before checking this invocation.
        subprocess.run(["journalctl", "--user", "--sync"], check=True, capture_output=True, timeout=30)
        journal = subprocess.run(
            ["journalctl", "--user", "-u", dep.service_name,
             f"_SYSTEMD_INVOCATION_ID={invocation}", "-o", "cat", "--no-pager"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        events = []
        for line in journal.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("run_id") == invocation:
                events.append(event)
        assert events, journal.stdout
        assert events[0]["event"] == "run_started"
        assert events[-1]["event"] == "run_completed"
        assert events[-1]["status"] == "succeeded"
        completed = [event for event in events if event["event"] == "task_completed"]
        assert {event["task"] for event in completed} == set(config["tasks"])
        assert all(event["status"] == "succeeded" for event in completed)
    finally:
        # Clean up even if installation, execution, or a journal assertion fails.
        command("uninstall")
        assert_clean()
