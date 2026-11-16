"""History reconstruction from journal records. Systemd's own messages come from
real captured fixtures (tests/fixtures); workflow events are synthesized."""

import json
from datetime import timedelta
from unittest.mock import create_autospec

import pytest
from journal_fakes import MANAGER, FakeJournal, Run, fixture

from floe.history import get_run, list_runs, run_logs
from floe.model import NotFoundError, ValidationError
from floe.systemd import Systemd


@pytest.fixture
def sd():
    sd = create_autospec(Systemd, instance=True)
    sd.show.return_value = {"ActiveState": "inactive", "InvocationID": ""}
    return sd


def test_real_success_fixture(sd):
    records = fixture("journal-mydag-success.jsonl")
    [run] = list_runs("mydag", FakeJournal(records), sd)
    assert run.run_id == "ca9f4c3263e046deb103494cd2d7d599"
    assert run.status == "succeeded" and run.error is None and run.result is None
    assert [(t.name, t.status) for t in run.tasks] == [
        ("fetch", "succeeded"), ("transform", "succeeded"), ("publish", "succeeded")]
    assert run.started.isoformat() == "2026-11-16T13:34:38.383202+00:00"
    assert run.duration == timedelta(microseconds=204)
    assert run.to_dict()["tasks"][0]["status"] == "succeeded"


def test_task_failure(sd):
    r = Run("a" * 32).starting().event("run_started").event("task_started", task="fetch")
    r.event("task_completed", task="fetch", status="failed", error="broken input")
    r.event("run_completed", status="failed", error="broken input").failed()
    [run] = list_runs("mydag", FakeJournal(r.records), sd)
    assert (run.status, run.error, run.result) == ("failed", "broken input", "exit-code")
    assert run.failed_task.name == "fetch" and run.failed_task.error == "broken input"


def test_timeout_is_incomplete_with_systemd_result(sd):
    real = fixture("journal-probe-timeout.jsonl")  # started, output, timed out, killed
    invocation = real[0]["USER_INVOCATION_ID"]
    r = Run(invocation, int(real[0]["__REALTIME_TIMESTAMP"]))
    r.event("run_started").event("task_started", task="fetch")
    records = [real[0], *r.records, *real[1:]]
    [run] = list_runs("mydag", FakeJournal(records), sd)
    assert run.status == "incomplete"
    assert run.result == "timeout"
    assert "systemd result: timeout" in run.error
    assert [(t.name, t.status) for t in run.tasks] == [("fetch", "incomplete")]
    assert run.finished is not None


def test_failure_before_run_started_uses_error_output(sd):
    real = fixture("journal-probe-fail.jsonl")  # printed "bad", "hello", exited 1
    [run] = list_runs("mydag", FakeJournal(real), sd)
    assert (run.status, run.error, run.result, run.tasks) == ("failed", "hello", "exit-code", ())

    r = Run("b" * 32).starting().output("error: invalid workflow:").output("  - task 'a' is not callable").failed()
    [run] = list_runs("mydag", FakeJournal(r.records), sd)
    assert run.error == "invalid workflow:\n  - task 'a' is not callable"


def test_running_and_vanished_runs(sd):
    r = Run("c" * 32).starting().event("run_started").event("task_started", task="fetch")
    sd.show.return_value = {"ActiveState": "active", "InvocationID": "c" * 32}
    [run] = list_runs("mydag", FakeJournal(r.records), sd)
    assert run.status == "running" and run.finished is None and run.error is None
    assert run.tasks[0].status == "running"

    sd.show.return_value = {"ActiveState": "inactive", "InvocationID": "c" * 32}
    [run] = list_runs("mydag", FakeJournal(r.records), sd)
    assert run.status == "incomplete" and run.tasks[0].status == "incomplete"

    starting_only = Run("d" * 32).starting()
    [run] = list_runs("mydag", FakeJournal(starting_only.records), sd)
    assert (run.status, run.error) == ("incomplete", "no completion recorded")


def journal_of(*ids):
    records, us = [], 1_800_000_000_000_000
    for invocation in ids:
        run = Run(invocation, us).succeeded_run()
        records += run.records
        us = run.us + 1_000_000
    return FakeJournal(records)


def test_list_runs_newest_first_with_limit_reading_lazily(sd):
    ids = [f"{n:x}" * 32 for n in range(1, 6)]
    journal = journal_of(*ids)
    runs = list_runs("mydag", journal, sd, since="today", limit=2)
    assert [run.run_id for run in runs] == [ids[4], ids[3]]
    assert journal.calls == [("mydag.service", "today", True)]
    per_run = len(journal.records_list) // 5
    assert journal.read == 2 * per_run + 1  # stopped at the first record of the third run
    assert len(list_runs("mydag", journal, sd, limit=None)) == 5
    with pytest.raises(ValidationError):
        list_runs("mydag", journal, sd, limit=0)
    assert list_runs("mydag", FakeJournal([]), sd) == []


def test_records_without_invocation_are_ignored(sd):
    journal = journal_of("e" * 32)
    journal.records_list.insert(0, {**MANAGER, "__REALTIME_TIMESTAMP": "1", "MESSAGE": "unrelated"})
    assert [run.run_id for run in list_runs("mydag", journal, sd)] == ["e" * 32]


def test_get_run_by_prefix_latest_and_errors(sd):
    first, second, other = "ab" + "1" * 30, "ab" + "2" * 30, "cd" + "3" * 30
    journal = journal_of(first, second, other)
    assert get_run("mydag", None, journal, sd).run_id == other
    assert get_run("mydag", "ab1", journal, sd).run_id == first
    assert get_run("mydag", second, journal, sd).run_id == second
    with pytest.raises(NotFoundError, match="ambiguous"):
        get_run("mydag", "ab", journal, sd)
    with pytest.raises(NotFoundError, match="matches 'ff'"):
        get_run("mydag", "ff", journal, sd)
    with pytest.raises(NotFoundError, match="no runs"):
        get_run("mydag", None, FakeJournal([]), sd)
    for bad in ["XYZ", "", "a" * 33, "--since"]:
        with pytest.raises(ValidationError):
            get_run("mydag", bad, journal, sd)


def test_run_logs_lines_and_task_attribution(sd):
    r = Run("f" * 32).starting().event("run_started").event("task_started", task="fetch")
    r.output("fetching").output(json.dumps({"schema": 1, "event": "run_completed", "workflow": "mydag",
                                            "run_id": "spoofed", "timestamp": "x", "status": "succeeded"}))
    r.event("task_completed", task="fetch", status="succeeded").output("between tasks")
    r.event("run_completed", status="succeeded").finished()
    r.records[3]["MESSAGE"] = list(b"bytes \xff")  # journald's form for non-UTF-8 messages
    logs = run_logs("mydag", None, FakeJournal(r.records), sd)
    assert logs.run.status == "succeeded"
    lines = [(line.kind, line.task, line.event.event if line.event else line.message) for line in logs.lines]
    assert lines == [
        ("systemd", None, "Starting mydag.service - Floe workflow mydag..."),
        ("event", None, "run_started"),
        ("event", "fetch", "task_started"),
        ("output", "fetch", "bytes \ufffd"),
        ("output", "fetch", r.records[4]["MESSAGE"]),  # an event for another run is just output
        ("event", "fetch", "task_completed"),
        ("output", None, "between tasks"),
        ("event", None, "run_completed"),
        ("systemd", None, "Finished mydag.service - Floe workflow mydag."),
    ]
    assert logs.to_dict()["lines"][1]["event"]["event"] == "run_started"
