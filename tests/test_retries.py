"""Real Tenacity decisions, deterministic waits, and journal/UI round trips."""

import asyncio
import json
import os
import sys
import subprocess
from concurrent.futures import CancelledError
from datetime import datetime
from unittest.mock import create_autospec

import pytest
from journal_fakes import FakeJournal, Run as Records

import floe
from floe_cli.main import _describe, _detail
from floe_web import render


@pytest.fixture
def waits(monkeypatch):
    delays = []
    # The clock/wait boundary only: Tenacity still makes all retry decisions.
    monkeypatch.setattr("tenacity.nap.time.sleep", delays.append)
    return delays


def workflow(task, policy=None, calls=None):
    calls = [] if calls is None else calls
    return floe.Workflow("mydag", {"before": lambda: calls.append("before"), "fetch": task,
                                   "later": lambda: calls.append("later")},
                         {"before": set(), "fetch": {"before"}, "later": {"fetch"}},
                         retries={"fetch": policy or floe.RetryPolicy(3, 5, (TimeoutError,))})


def test_eventual_success_preserves_prerequisites_and_attempts(waits):
    calls = []
    count = 0

    def fetch():
        nonlocal count
        count += 1
        calls.append(f"fetch {count}")
        if count < 3:
            raise TimeoutError(f"failure {count}")

    w = workflow(fetch, calls=calls)
    events = []
    assert floe.run(w, events.append)
    assert calls == ["before", "fetch 1", "fetch 2", "fetch 3", "later"]
    assert waits == [5, 5]
    assert [(e.attempt, e.status, e.error) for e in events if e.event == "attempt_completed"] == [
        (1, "failed", "failure 1"), (2, "failed", "failure 2"), (3, "succeeded", None)]
    assert sum(e.event == "task_started" and e.task == "fetch" for e in events) == 1
    assert sum(e.event == "task_completed" and e.task == "fetch" for e in events) == 1
    assert all(floe.Event.from_dict(e.to_dict()) == e for e in events)


@pytest.mark.parametrize("limit,error,expected,reason", [
    (3, TimeoutError, 3, "exhausted"), (1, TimeoutError, 1, "exhausted"),
    (3, ValueError, 1, "non_retryable"),
])
def test_final_failure_stops_independent_and_dependent_tasks(waits, limit, error, expected, reason):
    calls = []

    def fail():
        calls.append("fetch")
        raise error("broken")

    w = floe.Workflow("mydag", {"fetch": fail, "independent": lambda: calls.append("independent"),
                                "dependent": lambda: calls.append("dependent")},
                      {"fetch": set(), "independent": set(), "dependent": {"fetch"}},
                      retries={"fetch": floe.RetryPolicy(limit, 2, (TimeoutError,))})
    events = []
    assert not floe.run(w, events.append)
    assert calls == ["fetch"] * expected
    assert waits == [2] * (expected - 1)
    assert events[-2].failure_reason == reason
    assert events[-1].status == "failed"


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit, asyncio.CancelledError, CancelledError])
def test_interrupts_are_never_retried_and_environment_is_restored(waits, monkeypatch, error):
    monkeypatch.setenv("FLOE_RUN_ID", "outer")
    calls = []

    def fail():
        calls.append(1)
        raise error()

    with pytest.raises(error):
        floe.run(workflow(fail, floe.RetryPolicy(3, 0, (Exception,))), lambda e: None)
    assert calls == [1] and waits == []
    assert os.environ["FLOE_RUN_ID"] == "outer"


def test_cancellation_during_wait_stops_without_another_attempt(monkeypatch):
    calls = []
    events = []

    def wait(seconds):
        assert seconds == 5
        raise KeyboardInterrupt

    def fail():
        calls.append(1)
        raise TimeoutError("transient")

    monkeypatch.setattr("tenacity.nap.time.sleep", wait)
    with pytest.raises(KeyboardInterrupt):
        floe.run(workflow(fail), events.append)
    assert calls == [1]
    assert events[-1].event == "retry_scheduled"
    assert not any(e.event == "run_completed" for e in events)


@pytest.mark.parametrize("kind", ["attempt_started", "attempt_completed", "retry_scheduled", "task_completed"])
def test_callback_errors_are_not_task_failures(waits, kind):
    calls = []
    events = []

    def fail():
        calls.append(1)
        raise TimeoutError("transient")

    def callback(event):
        events.append(event)
        if event.event == kind and event.task == "fetch":
            raise TimeoutError("event sink broken")

    with pytest.raises(TimeoutError, match="event sink broken"):
        floe.run(workflow(fail), callback)
    assert len(calls) == (0 if kind == "attempt_started" else 3 if kind == "task_completed" else 1)
    assert not any(e.event == "run_completed" for e in events)


def test_async_return_validation_is_not_retried(waits):
    calls = []

    async def async_work():
        pass

    def task():
        calls.append(1)
        return async_work()

    events = []
    assert not floe.run(workflow(task, floe.RetryPolicy(3, 0, (Exception,))), events.append)
    assert calls == [1] and waits == []
    assert events[-2].failure_reason == "non_retryable"
    assert "must be synchronous" in events[-1].error


def test_zero_delay_and_unconfigured_event_contract(waits):
    calls = []

    def task():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError()

    assert floe.run(workflow(task, floe.RetryPolicy(2, 0, (TimeoutError,))), lambda e: None)
    assert waits == [0]
    events = []
    assert floe.run(floe.Workflow("demo", {"a": lambda: None}, {"a": set()}), events.append)
    assert [e.event for e in events] == ["run_started", "task_started", "task_completed", "run_completed"]
    assert {e.schema for e in events} == {1}


@pytest.mark.parametrize("policy,match", [
    (floe.RetryPolicy(True, 0, (Exception,)), "max_attempts"),
    (floe.RetryPolicy(0, 0, (Exception,)), "max_attempts"),
    (floe.RetryPolicy(1.0, 0, (Exception,)), "max_attempts"),
    (floe.RetryPolicy(2, -1, (Exception,)), "delay_seconds"),
    (floe.RetryPolicy(2, float("nan"), (Exception,)), "delay_seconds"),
    (floe.RetryPolicy(2, float("inf"), (Exception,)), "delay_seconds"),
    (floe.RetryPolicy(2, 10**1000, (Exception,)), "delay_seconds"),
    (floe.RetryPolicy(2, True, (Exception,)), "delay_seconds"),
    (floe.RetryPolicy(2, 0, (BaseException,)), "retry_on"),
    (floe.RetryPolicy(2, 0, (KeyboardInterrupt,)), "retry_on"),
    (floe.RetryPolicy(2, 0, ("TimeoutError",)), "retry_on"),
    (floe.RetryPolicy(2, 0, ()), "retry_on"),
    (floe.RetryPolicy(2, 0, None), "retry_on"),
    (object(), "RetryPolicy"),
])
def test_policy_errors_are_aggregated_with_definition_errors(policy, match):
    with pytest.raises(floe.ValidationError) as caught:
        floe.Workflow("demo", {"a": 42}, {"a": {"missing"}}, retries={"a": policy, "unknown": policy})
    message = str(caught.value)
    assert match in message and "not callable" in message and "unknown prerequisites" in message
    assert "unknown task 'unknown'" in message


def test_policy_and_mapping_are_snapshots():
    types = [TimeoutError]
    policy = floe.RetryPolicy(2, 0, iter(types))
    policies = {"a": policy}
    w = floe.Workflow("demo", {"a": lambda: None}, {"a": set()}, retries=policies)
    types.append(ValueError)
    policies.clear()
    assert w.retries["a"] is policy and policy.retry_on == (TimeoutError,)
    assert w.to_dict()["retries"]["a"]["retry_on"] == ["builtins.TimeoutError"]


def test_sibling_isolation_on_each_attempt_and_after_failure(tmp_path, waits):
    script = tmp_path / "flow.py"
    (tmp_path / "retry_helper.py").write_text("count = 0\n")
    script.write_text('''from floe import RetryPolicy
WORKFLOW = "mydag"
def fetch():
    import retry_helper
    retry_helper.count += 1
    if retry_helper.count < 2:
        raise TimeoutError("try again")
tasks = {"fetch": fetch}
dag = {"fetch": set()}
retries = {"fetch": RetryPolicy(2, 0, (TimeoutError,))}
''')
    path, modules = list(sys.path), dict(sys.modules)
    first, second = floe.load(script), floe.load(script)
    for w in (first, second):
        events = []
        assert floe.run(w, events.append)
        assert [e.status for e in events if e.event == "attempt_completed"] == ["failed", "succeeded"]
        assert sys.path == path
        assert sys.modules.get("retry_helper") is modules.get("retry_helper")
    assert waits == [0, 0]


def retry_records():
    r = Records("a" * 32).event("run_started", schema=2).event("task_started", schema=2, task="fetch")
    fields = dict(schema=2, task="fetch", max_attempts=3)
    r.event("attempt_started", attempt=1, **fields).output("first output")
    r.event("attempt_completed", attempt=1, status="failed", error="temporary <bad>", **fields)
    r.event("retry_scheduled", attempt=1, delay_seconds=5, **fields)
    r.event("attempt_started", attempt=2, **fields).output("second output")
    r.event("attempt_completed", attempt=2, status="succeeded", **fields)
    r.event("task_completed", schema=2, task="fetch", status="succeeded")
    r.event("run_completed", schema=2, status="succeeded")
    return r


def history(records, *, running=False, max_records=None):
    sd = create_autospec(floe.Systemd, instance=True)
    sd.show.return_value = {"ActiveState": "active" if running else "inactive", "InvocationID": "a" * 32}
    return floe.run_logs("mydag", None, FakeJournal(records), sd, max_records=max_records)


def test_history_round_trip_retains_failed_attempt_and_output():
    logs = history(retry_records().records)
    task, = logs.run.tasks
    assert task.status == logs.run.status == "succeeded"
    assert [(a.number, a.status, a.error) for a in task.attempts] == [
        (1, "failed", "temporary <bad>"), (2, "succeeded", None)]
    assert task.retry_at is None
    assert [(l.message, l.task, l.attempt) for l in logs.lines if l.kind == "output"] == [
        ("first output", "fetch", 1), ("second output", "fetch", 2)]
    assert logs.to_dict()["run"]["tasks"][0]["attempts"][0]["error"] == "temporary <bad>"
    assert "succeeded after 2 attempts" in _detail(logs.run)
    assert "[fetch attempt 1] first output" in [_describe(l) for l in logs.lines]
    page = render.run_detail(logs)
    assert "Succeeded after 2 attempts" in page and "Attempt history" in page
    assert "temporary &lt;bad&gt;" in page and "temporary <bad>" not in page
    assert "fetch · attempt 2" in page


@pytest.mark.parametrize("cut,waiting", [(6, True), (7, False)])
def test_active_wait_and_attempt_states_then_interruption(cut, waiting):
    records = retry_records().records[:cut]
    active = history(records, running=True)
    task, = active.run.tasks
    assert task.status == "running"
    assert (task.retry_at is not None) is waiting
    assert ("waiting to retry" if waiting else "attempt 2/3") in _detail(active.run)
    assert ("Waiting to retry" if waiting else "Attempt 2/3") in render.run_detail(active)
    ended = history(records).run
    assert ended.status == ended.tasks[0].status == "incomplete"
    assert ended.tasks[0].failure_reason is None
    assert ended.tasks[0].retry_at is None
    assert ended.tasks[0].attempts[-1].status == ("failed" if waiting else "incomplete")


def test_truncated_wait_without_attempt_records_is_visible():
    logs = history(retry_records().records[:6], running=True, max_records=1)
    task, = logs.run.tasks
    assert logs.run.truncated and task.started is None
    assert task.attempts == () and task.max_attempts == 3
    assert task.retry_at is not None
    assert "waiting to retry" in _detail(logs.run)
    assert "Waiting to retry" in render.run_detail(logs)
    assert "Attempt history" not in render.run_detail(logs)


def test_truncated_history_does_not_invent_attempts_or_starts():
    logs = history(retry_records().records, max_records=4)
    task, = logs.run.tasks
    assert logs.run.truncated and logs.run.started is None
    assert task.started is None and task.duration is None
    assert len(task.attempts) == 1
    attempt, = task.attempts
    assert attempt.number == 2 and attempt.started is None and attempt.status == "succeeded"


def test_old_history_has_no_invented_attempts():
    logs = history(Records("a" * 32).succeeded_run().records)
    assert all(t.attempts == () and t.max_attempts is None for t in logs.run.tasks)
    assert all(l.attempt is None for l in logs.lines)


@pytest.mark.parametrize("changes", [
    {"schema": 1}, {"attempt": True}, {"attempt": 0}, {"attempt": 4},
    {"max_attempts": None}, {"max_attempts": 2.0}, {"task": None},
    {"status": "failed"}, {"delay_seconds": 1}, {"failure_reason": "exhausted"},
])
def test_malformed_attempt_events_remain_output(changes):
    event = floe.Event("attempt_started", "mydag", "a" * 32, datetime.now().astimezone().isoformat(),
                       task="fetch", attempt=1, max_attempts=3)
    data = {**event.to_dict(), **changes}
    assert floe.Event.from_dict(data) is None
    r = Records("a" * 32).output(json.dumps(data))
    assert history(r.records).lines[0].kind == "output"


@pytest.mark.parametrize("delay", [None, -1, True, float("nan"), float("inf"), 10**1000])
def test_invalid_retry_wait_is_not_history_evidence(delay):
    event = floe.Event("retry_scheduled", "mydag", "a" * 32, datetime.now().astimezone().isoformat(),
                       task="fetch", attempt=1, max_attempts=3, delay_seconds=delay)
    assert floe.Event.from_dict(event.to_dict()) is None


def test_retry_cannot_be_scheduled_after_the_last_attempt():
    event = floe.Event("retry_scheduled", "mydag", "a" * 32, datetime.now().astimezone().isoformat(),
                       task="fetch", attempt=3, max_attempts=3, delay_seconds=1)
    assert history(Records("a" * 32).output(json.dumps(event.to_dict())).records).lines[0].kind == "output"


@pytest.mark.parametrize("reason", ["exhausted", "non_retryable"])
def test_history_preserves_final_failure_reason(reason):
    r = retry_records()
    r.records = r.records[:5]
    r.event("task_completed", schema=2, task="fetch", status="failed", error="temporary <bad>",
            failure_reason=reason)
    r.event("run_completed", schema=2, status="failed", error="temporary <bad>")
    logs = history(r.records)
    task, = logs.run.tasks
    assert task.failure_reason == reason and task.status == "failed"
    assert reason in _detail(logs.run)
    assert reason.replace("_", " ") in render.run_detail(logs)
    assert task.attempts[0].error == "temporary <bad>"


def test_cli_merged_stream_preserves_attempt_output_attribution(tmp_path):
    script = tmp_path / "flow.py"
    script.write_text('''from floe import RetryPolicy
import subprocess
import os
WORKFLOW = "mydag"
count = 0
def fetch():
    global count
    count += 1
    print(f"printed {count}")
    subprocess.run(["echo", f"child {count}"], check=True)
    os.write(1, f"raw {count}\\n".encode())
    if count == 1: raise TimeoutError("transient")
tasks = {"fetch": fetch}
dag = {"fetch": set()}
retries = {"fetch": RetryPolicy(2, 0, (TimeoutError,))}
''')
    result = subprocess.run([sys.executable, "-m", "floe_cli", "run", str(script)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path, timeout=10)
    assert result.returncode == 0, result.stdout
    lines = result.stdout.splitlines()
    r = Records(json.loads(lines[0])["run_id"])
    for line in lines:
        r.output(line)
    sd = create_autospec(floe.Systemd, instance=True)
    sd.show.return_value = {"ActiveState": "inactive"}
    logs = floe.run_logs("mydag", None, FakeJournal(r.records), sd)
    assert logs.run.status == "succeeded"
    # Python buffering can put child/raw output before print; all must still
    # precede their attempt completion and retain the correct identity.
    output = [(l.message, l.attempt) for l in logs.lines if l.kind == "output"]
    assert sorted(output) == sorted((f"{kind} {number}", number)
                                    for number in (1, 2) for kind in ("printed", "child", "raw"))
    assert all(l.task == "fetch" for l in logs.lines if l.kind == "output")
