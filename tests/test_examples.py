"""The examples the documentation points at still work. No service manager involved."""

import json
import os
import runpy
import shutil
from pathlib import Path

import pytest

import floe
from floe_cli.main import main

EXAMPLES = Path(__file__).parents[1] / "examples"


def test_example_workflow_runs(capsys):
    workflow = floe.load(EXAMPLES / "workflow.py")
    assert workflow.name == "mydag" and workflow.schedule
    assert main(["run", str(EXAMPLES / "workflow.py")]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["event"] == "run_completed" and events[-1]["status"] == "succeeded"
    completed = [e["task"] for e in events if e["event"] == "task_completed"]
    assert completed == list(workflow.order)


def test_deploy_demo_plans_without_writing(capsys):
    runpy.run_path(str(EXAMPLES / "deploy_demo.py"), run_name="__main__")
    out = capsys.readouterr().out
    assert "# mydag.service: create" in out and "# mydag.timer: create" in out
    assert not floe.user_unit_dir().exists()


def _checksums(tmp_path):
    """A copy of the checksums example, so its state stays out of the repository."""
    directory = tmp_path / "checksums"
    shutil.copytree(EXAMPLES / "checksums", directory, ignore=shutil.ignore_patterns("state", "out", "watched"))
    (directory / "watched").mkdir()
    return directory


def _run(directory, capsys):
    status = main(["run", str(directory / "checksums.py")])
    out, err = capsys.readouterr()
    events = [json.loads(line) for line in out.splitlines()]  # stdout is only events
    return status, events, err


def _report(directory):
    path = directory / "out" / "changes.log"
    return [line.split(" ", 2)[2] for line in path.read_text().splitlines()] if path.exists() else []


def test_checksums_example_reports_each_change_once(tmp_path, capsys):
    directory = _checksums(tmp_path)
    watched = directory / "watched"
    (watched / "a.txt").write_text("one")
    (watched / "sub").mkdir()
    (watched / "sub" / "b.txt").write_text("two")

    assert _run(directory, capsys)[0] == 0
    assert _report(directory) == ["added a.txt", "added sub/b.txt"]

    status, events, err = _run(directory, capsys)  # nothing changed: nothing reported again
    assert status == 0 and "0 added, 0 changed, 0 removed" in err
    assert _report(directory) == ["added a.txt", "added sub/b.txt"]

    (watched / "a.txt").write_text("changed")
    (watched / "sub" / "b.txt").unlink()
    (watched / "c.txt").write_text("three")
    assert _run(directory, capsys)[0] == 0
    assert _report(directory)[2:] == ["added c.txt", "changed a.txt", "removed sub/b.txt"]


def test_checksums_example_fails_clearly_without_its_directory(tmp_path, capsys):
    directory = _checksums(tmp_path)
    (directory / "watched").rmdir()
    status, events, err = _run(directory, capsys)
    assert status == 1
    assert events[-1]["error"].startswith("directory to watch not found")
    assert [e["task"] for e in events if e["event"] == "task_completed"] == ["preflight"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read files without read permission")
def test_checksums_example_finishes_other_files_then_fails(tmp_path, capsys):
    directory = _checksums(tmp_path)
    watched = directory / "watched"
    (watched / "ok.txt").write_text("fine")
    (watched / "locked.txt").write_text("secret")
    assert _run(directory, capsys)[0] == 0

    (watched / "ok.txt").write_text("edited")
    (watched / "locked.txt").chmod(0)
    try:
        status, events, err = _run(directory, capsys)
    finally:
        (watched / "locked.txt").chmod(0o600)
    assert status == 1
    failed = [e for e in events if e["event"] == "task_completed" and e["status"] == "failed"]
    assert [(e["task"], e["error"]) for e in failed] == [("finish", "1 files could not be read: locked.txt")]
    # The readable file was still reported; the unreadable one is not "removed".
    assert _report(directory)[2:] == ["changed ok.txt"]

    (watched / "locked.txt").write_text("secret, changed")
    assert _run(directory, capsys)[0] == 0  # compared against its last known checksum
    assert _report(directory)[3:] == ["changed locked.txt"]


def test_checksums_example_rejects_state_from_an_overlapping_run(tmp_path, monkeypatch):
    """A direct run overlapping a scheduled one can overwrite state/scan.json
    between this run's tasks; using it would report the other run's scan."""
    directory = _checksums(tmp_path)
    (directory / "watched" / "a.txt").write_text("one")
    workflow = floe.load(directory / "checksums.py")
    monkeypatch.setenv("FLOE_RUN_ID", "other-run")
    workflow.tasks["scan"]()
    monkeypatch.setenv("FLOE_RUN_ID", "this-run")
    with pytest.raises(RuntimeError, match="state/scan.json was not written by this run"):
        workflow.tasks["compare"]()
    assert not (directory / "state" / "changes.json").exists()
