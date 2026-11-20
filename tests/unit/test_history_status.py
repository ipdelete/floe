"""History turns a run's journal records into a status.

The journal and service manager are faked; record shapes are copied from real
user-systemd Type=oneshot runs (the unit type floe installs).
"""

import json
from datetime import UTC, datetime, timedelta

import floe

WORKFLOW = "nightly"
RUN_ID = "ff55c14097a84a46bec1b4bd8f85d06b"
OTHER_RUN_ID = "3db6e561175e45ee87f34971e0a32fd8"
START = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)


class FakeJournal:
    """journalctl --user --unit=... --output=json, as Journal.records exposes it."""

    def __init__(self, records):
        self._records = records

    def records(self, unit, *, since=None, reverse=False, invocations=None, limit=None):
        selected = [r for r in self._records
                    if invocations is None
                    or (r.get("_SYSTEMD_INVOCATION_ID") or r.get("USER_INVOCATION_ID")) in invocations]
        if reverse:
            selected.reverse()
        return iter(selected if limit is None else selected[:limit])


class FakeSystemd:
    """systemctl --user show for the workflow's service."""

    def __init__(self, active_state="inactive", invocation=""):
        self._props = {"ActiveState": active_state, "InvocationID": invocation}

    def show(self, unit, *props, unix_timestamps=False):
        return {prop: self._props.get(prop, "") for prop in props}


def at(step):
    return START + timedelta(milliseconds=step)


def realtime(step):
    return str((at(step) - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1))


# What the run itself wrote (journald adds _SYSTEMD_INVOCATION_ID).

def event(step, name, run_id=RUN_ID, **fields):
    payload = {"schema": 1, "event": name, "workflow": WORKFLOW, "run_id": run_id,
               "timestamp": at(step).isoformat(), **fields}
    return {"__REALTIME_TIMESTAMP": realtime(step), "PRIORITY": "6",
            "_SYSTEMD_INVOCATION_ID": RUN_ID, "MESSAGE": json.dumps(payload)}


def output(step, text):
    return {"__REALTIME_TIMESTAMP": realtime(step), "PRIORITY": "6",
            "_SYSTEMD_INVOCATION_ID": RUN_ID, "MESSAGE": text}


# What the service manager wrote about the run (USER_INVOCATION_ID only).

def manager(step, message, message_id, priority="6", **fields):
    return {"__REALTIME_TIMESTAMP": realtime(step), "PRIORITY": priority,
            "USER_INVOCATION_ID": RUN_ID, "MESSAGE_ID": message_id,
            "MESSAGE": f"{WORKFLOW}.service: {message}", **fields}


def starting(step):
    return manager(step, "Starting", "7d4958e842da4a758f6c1cdc7b36dcc5")


def finished(step):
    return manager(step, "Finished", "39f53479d3a045ac8e11786248231fbf", JOB_RESULT="done")


def exited_with_failure(step):
    return manager(step, "Main process exited, code=exited, status=1/FAILURE",
                   "98e322203f7a4ed290d09fe03c09fe15", priority="5")


def failed(step, result):
    return manager(step, f"Failed with result '{result}'.", "d9b373ed55a64feb8242e02dbe79a49c",
                   priority="4", UNIT_RESULT=result)


def failed_to_start(step):
    return manager(step, "Failed to start", "be02cf6855d2428ba40df7e9d022f03d",
                   priority="3", JOB_RESULT="failed")


def read_run(records, sd=None):
    return floe.get_run(WORKFLOW, RUN_ID, FakeJournal(records), sd or FakeSystemd())


def test_completed_run_takes_its_status_from_the_completion_event():
    run = read_run([
        starting(0),
        event(1, "run_started"),
        event(2, "task_started", task="a"),
        event(3, "task_completed", task="a", status="succeeded"),
        event(4, "run_completed", status="succeeded"),
        finished(5),
    ])

    assert run.status == "succeeded"


def test_active_run_without_completion_is_running():
    run = read_run([
        starting(0),
        event(1, "run_started"),
        event(2, "task_started", task="a"),
    ], FakeSystemd(active_state="activating", invocation=RUN_ID))

    assert run.status == "running"


def test_run_that_ended_without_completion_is_incomplete():
    # e.g. TimeoutStartSec killed the run mid-task.
    run = read_run([
        starting(0),
        event(1, "run_started"),
        event(2, "task_started", task="a"),
        failed(3, "timeout"),
        failed_to_start(4),
    ])

    assert run.status == "incomplete"


def bad_definition_run():
    # The definition raised on import: floe printed an error before any event.
    return read_run([
        starting(0),
        output(1, "error: /tmp/bad.py: RuntimeError: bad definition"),
        exited_with_failure(2),
        failed(3, "exit-code"),
        failed_to_start(4),
    ])


def test_service_failure_before_any_event_is_failed():
    assert bad_definition_run().status == "failed"


def test_service_failure_before_any_event_reports_the_printed_error():
    assert bad_definition_run().error == "/tmp/bad.py: RuntimeError: bad definition"


def test_service_success_without_events_is_incomplete():
    # The service exited 0 but never ran the workflow (e.g. a wrong ExecStart).
    run = read_run([
        starting(0),
        output(1, "usage: floe [-h] COMMAND ..."),
        finished(2),
    ])

    assert run.status == "incomplete"


def test_events_with_another_run_id_are_not_trusted():
    # Same journal stream, but the events claim a different run (e.g. a child workflow).
    run = read_run([
        starting(0),
        event(1, "run_started", run_id=OTHER_RUN_ID),
        event(2, "run_completed", run_id=OTHER_RUN_ID, status="succeeded"),
        finished(3),
    ])

    assert run.status == "incomplete"
