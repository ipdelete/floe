"""Opt-in test of the actual workflow and user systemd session.

This deploys disposable workflow units, executes the tasks, and always attempts
to uninstall the units afterward. Normal pytest runs skip this module's test.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import floe

pytestmark = pytest.mark.systemd_e2e
ROOT = Path(__file__).parents[1]


def test_clean_redeploy_run_and_journal():
    script = ROOT / "workflow.py"
    workflow = floe.load(script)
    dep = floe.Deployment.for_workflow(workflow)
    store = floe.UnitStore(floe.user_unit_dir())
    sd = floe.Systemd()

    def floe_command(*args):
        # The real `floe` command from this environment, as a user would run it.
        result = subprocess.run(
            [str(Path(sys.executable).parent / "floe"), *args],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def command(name, *args):
        return floe_command(name, str(script), *args)

    def latest_run_id():
        subprocess.run(["journalctl", "--user", "--sync"], check=True, capture_output=True, timeout=30)
        runs = json.loads(floe_command("runs", workflow.name, "--json", "-n", "1"))
        return runs[0]["run_id"] if runs else None

    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    stamp = data_home / "systemd" / "timers" / f"stamp-{dep.timer_name}"

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
            state = sd.show(name, "LoadState", "ActiveState")
            assert state == {"LoadState": "not-found", "ActiveState": "inactive"}, (name, state)
            for suffix in ("wants", "requires", "upholds"):
                for directory in store.path.glob(f"*.{suffix}"):
                    assert not (directory / name).is_symlink()
        # A leftover stamp would make the next install "catch up" with a run at once.
        assert not stamp.exists(), f"test left {stamp}"
        assert workflow.name not in [w["name"] for w in json.loads(floe_command("list", "--json"))]

    try:
        command("uninstall")
        assert_clean()

        before = latest_run_id()
        command("install")
        assert latest_run_id() == before, "install triggered a run"
        [listed] = [w for w in json.loads(floe_command("list", "--json", "--check")) if w["name"] == workflow.name]
        assert (listed["script"], listed["problems"], listed["enabled"]) == (str(script), [], True)
        assert listed["next_run"]
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
        assert [event["task"] for event in completed] == list(workflow.order)
        assert all(event["status"] == "succeeded" for event in completed)

        # History, reconstructed by floe from the same journal records.
        runs = json.loads(command("runs", "--json", "-n", "1"))
        assert [(run["run_id"], run["status"]) for run in runs] == [(invocation, "succeeded")]
        assert [task["name"] for task in runs[0]["tasks"]] == list(workflow.order)
        logs = json.loads(command("logs", invocation[:8], "--json"))
        assert logs["run"]["run_id"] == invocation
        kinds = [line["kind"] for line in logs["lines"]]
        assert kinds[0] == kinds[-1] == "systemd" and "event" in kinds and "output" in kinds
        assert "■ run succeeded" in command("logs")
    finally:
        # Clean up even if installation, execution, or a journal assertion fails,
        # and verify cleanup even if uninstall itself reports an error.
        try:
            command("uninstall")
        finally:
            assert_clean()
