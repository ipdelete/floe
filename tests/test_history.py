"""History reconstruction from journal records. Systemd's own messages come from
real captured fixtures (tests/fixtures); workflow events are synthesized."""

import json
from datetime import UTC, datetime, timedelta
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


@pytest.mark.parametrize("status,error", [("succeeded", None), ("failed", "broken input")])
def test_task_completion_without_start_preserves_recorded_fields(sd, status, error):
    r = Run("a" * 32).event("task_completed", task="fetch", status=status, error=error)
    r.event("run_completed", status=status, error=error)
    if status == "failed":
        r.failed()
    else:
        r.finished()
    [run] = list_runs("mydag", FakeJournal(r.records), sd)
    [task] = run.tasks
    assert (run.status, task.name, task.status, task.error) == (status, "fetch", status, error)
    assert task.started is None and task.duration is None
    assert task.finished is not None


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


@pytest.mark.parametrize("message_id,fields", [
    ("be02cf6855d2428ba40df7e9d022f03d", {}),
    ("39f53479d3a045ac8e11786248231fbf", {"JOB_RESULT": "failed"}),
    ("d9b373ed55a64feb8242e02dbe79a49c", {"UNIT_RESULT": "exit-code"}),
])
def test_explicit_manager_failure_without_run_events_is_failed(sd, message_id, fields):
    r = Run("a" * 32).output("error: could not start")
    r.systemd("Failed", MESSAGE_ID=message_id, **fields)
    [run] = list_runs("mydag", FakeJournal(r.records), sd)
    assert (run.status, run.error, run.tasks) == ("failed", "could not start", ())


def test_task_evidence_without_run_events_is_incomplete_on_failure(sd):
    r = Run("a" * 32).event("task_started", task="fetch").failed()
    [run] = list_runs("mydag", FakeJournal(r.records), sd)
    assert run.status == "incomplete"
    assert run.tasks[0].status == "incomplete"
    assert run.finished is not None


@pytest.mark.parametrize("keep_tasks", [False, True])
@pytest.mark.parametrize("terminal", ["finished", "stopped", "success"])
def test_successful_terminal_without_run_events_is_incomplete(sd, keep_tasks, terminal):
    r = Run("a" * 32)
    if keep_tasks:
        r.event("task_started", task="fetch").event("task_completed", task="fetch", status="succeeded")
    if terminal == "finished":
        r.finished()
    elif terminal == "stopped":
        r.systemd("Stopped mydag.service", MESSAGE_ID="39f53479d3a045ac8e11786248231fbf",
                  JOB_TYPE="stop", JOB_RESULT="done")
    else:
        r.systemd("Deactivated successfully", MESSAGE_ID="7ad2d189f7e94e70a38c781354912448",
                  UNIT_RESULT="success")
    [run] = list_runs("mydag", FakeJournal(r.records), sd)
    assert run.status == "incomplete"
    assert run.error == "no completion recorded"
    assert run.finished is not None
    assert run.to_dict()["status"] == "incomplete"


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


def test_list_runs_orders_candidates_and_fetches_complete_invocations(sd):
    ids = [f"{n:x}" * 32 for n in range(1, 6)]
    journal = journal_of(*ids)
    runs = list_runs("mydag", journal, sd, since="today", limit=2)
    assert [run.run_id for run in runs] == [ids[4], ids[3]]
    assert journal.calls == [
        ("mydag.service", "today", True, None),
        ("mydag.service", None, False, tuple(reversed(ids))),
    ]
    journal.calls.clear()
    assert [r.run_id for r in list_runs("mydag", journal, sd, limit=2)] == [ids[4], ids[3]]
    assert journal.calls[-1] == ("mydag.service", None, False, (ids[4], ids[3]))
    assert len(list_runs("mydag", journal, sd, limit=None)) == 5
    with pytest.raises(ValidationError):
        list_runs("mydag", journal, sd, limit=0)
    assert list_runs("mydag", FakeJournal([]), sd) == []


def test_large_history_requests_return_every_run(sd):
    ids = [f"{index:032x}" for index in range(150)]
    runs = list_runs("mydag", journal_of(*ids), sd, limit=None)
    assert [run.run_id for run in runs] == ids[::-1]
    assert all(run.status == "succeeded" for run in runs)


def test_full_id_lookup_fetches_only_that_invocation(sd):
    journal = journal_of("a" * 32, "b" * 32)
    run = get_run("mydag", "a" * 32, journal, sd)
    assert run.run_id == "a" * 32 and run.status == "succeeded"
    assert journal.calls == [("mydag.service", None, False, ("a" * 32,))]


@pytest.mark.parametrize("late_after_completion", [False, True])
def test_interleaved_invocations_have_complete_consistent_history(sd, late_after_completion):
    old = Run("a" * 32).succeeded_run(tasks=("fetch",))
    new = Run("b" * 32, old.us + 1_000_000).succeeded_run(tasks=("fetch",))
    position = len(new.records) if late_after_completion else 4
    late = {**old.records[3], "MESSAGE": "delayed output",
            "__REALTIME_TIMESTAMP": str(int(new.records[position - 1]["__REALTIME_TIMESTAMP"]) + 1)}
    journal = FakeJournal([*old.records, *new.records[:position], late, *new.records[position:]])
    expected = get_run("mydag", new.id, journal, sd)
    assert expected.tasks[0].started is not None
    assert get_run("mydag", None, journal, sd) == expected
    assert get_run("mydag", "bbbb", journal, sd) == expected
    assert list_runs("mydag", journal, sd, limit=1) == [expected]
    assert [r.run_id for r in list_runs("mydag", journal, sd)] == [new.id, old.id]
    assert len(run_logs("mydag", None, journal, sd).lines) == len(new.records)


def test_since_selects_complete_runs_even_when_their_starts_precede_the_window(sd):
    old = Run("a" * 32).succeeded_run(tasks=("fetch",))
    new = Run("b" * 32, old.us + 1_000_000).succeeded_run(tasks=("fetch",))
    late = {**old.records[3], "__REALTIME_TIMESTAMP": str(new.us + 1)}
    cutoff = int(new.records[0]["__REALTIME_TIMESTAMP"])

    class WindowJournal(FakeJournal):
        def records(self, unit, *, since=None, **kwargs):
            for record in super().records(unit, since=since, **kwargs):
                if since is None or int(record["__REALTIME_TIMESTAMP"]) >= cutoff:
                    yield record

    journal = WindowJournal([*old.records, *new.records, late])
    expected = get_run("mydag", new.id, journal, sd)
    assert list_runs("mydag", journal, sd, since="recent", limit=1) == [expected]
    both = list_runs("mydag", journal, sd, since="recent", limit=None)
    assert [run.run_id for run in both] == [new.id, old.id]
    assert all(run.tasks[0].started is not None for run in both)


@pytest.mark.parametrize("fields", [
    {"event": "run_started", "timestamp": "2026-01-01T00:00:00"},
    {"event": "run_completed", "status": "not-a-status"},
    {"event": "task_completed", "task": "fetch"},
])
def test_malformed_events_are_output_not_history_state(sd, fields):
    r = Run("a" * 32).succeeded_run(tasks=("fetch",))
    data = {"schema": 1, "workflow": "mydag", "run_id": r.id,
            "timestamp": "2026-01-01T00:00:00+00:00", **fields}
    r.output(json.dumps(data))
    logs = run_logs("mydag", None, FakeJournal(r.records), sd)
    assert logs.run.status == "succeeded"
    assert logs.run.tasks[0].status == "succeeded"
    assert logs.lines[-1].kind == "output"
    assert json.loads(json.dumps(logs.to_dict()))["run"]["duration"] is not None


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


def test_log_line_times_follow_the_journal_order(sd):
    """Events carry their own timestamp, taken before the line reached journald.
    Displaying it next to journald receive times made times go backwards."""
    r = Run("e" * 32).starting().event("run_started").event("task_started", task="fetch")
    r.output("fetching")
    early = "2027-01-15T08:00:00+00:00"  # before every journal record above
    r.event("task_completed", task="fetch", status="succeeded", timestamp=early)
    r.event("run_completed", status="succeeded").finished()
    logs = run_logs("mydag", None, FakeJournal(r.records), sd)
    times = [line.timestamp for line in logs.lines]
    assert times == sorted(times)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert times == [epoch + timedelta(microseconds=int(record["__REALTIME_TIMESTAMP"])) for record in r.records]
    completed = next(line for line in logs.lines if line.event and line.event.event == "task_completed")
    assert completed.event.timestamp == early  # the event's own time is kept
    assert logs.run.tasks[0].finished.isoformat() == early  # and still used for the run
