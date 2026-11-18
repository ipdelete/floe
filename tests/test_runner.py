import os
import subprocess

import pytest

from floe.model import Workflow
from floe.runner import run


def collect():
    events = []
    return events, events.append


def test_success_events_and_dependency_order():
    calls = []
    workflow = Workflow("test", {name: lambda n=name: calls.append(n) for name in ("b", "a")},
                        {"b": {"a"}, "a": set()})
    events, on_event = collect()
    assert run(workflow, on_event) is True
    assert calls == ["a", "b"]
    assert [e.event for e in events] == [
        "run_started", "task_started", "task_completed", "task_started", "task_completed", "run_completed"]
    assert [e.task for e in events if e.event == "task_started"] == ["a", "b"]
    assert events[-1].status == "succeeded"
    assert len({e.run_id for e in events}) == 1
    assert all(e.workflow == "test" and e.timestamp for e in events)


def test_failure_stops_dependents_and_independent_tasks():
    calls = []

    def fail():
        calls.append("fail")
        raise ValueError("broken input")

    # "independent" does not depend on "fail", but comes after it in the order.
    workflow = Workflow("test", {
        "fail": fail, "independent": lambda: calls.append("independent"),
        "dependent": lambda: calls.append("dependent"),
    }, {"fail": set(), "independent": set(), "dependent": {"fail"}})
    assert workflow.order.index("fail") < workflow.order.index("independent")
    events, on_event = collect()
    assert run(workflow, on_event) is False
    assert calls == ["fail"]
    assert [e.event for e in events] == ["run_started", "task_started", "task_completed", "run_completed"]
    assert events[2].task == "fail" and events[2].status == "failed"
    assert events[2].error == events[3].error == "broken input"


@pytest.mark.parametrize("kind", ["coroutine", "awaitable", "async_generator"])
def test_async_results_fail_without_running_later_tasks(kind):
    calls = []

    async def work():
        calls.append("awaited")

    class Awaitable:
        def __await__(self):
            calls.append("awaited")
            yield

    async def async_generator():
        calls.append("iterated")
        yield None

    result = {"coroutine": work, "awaitable": Awaitable, "async_generator": async_generator}[kind]()
    workflow = Workflow("test", {"first": lambda: result, "later": lambda: calls.append("later")},
                        {"first": set(), "later": {"first"}})
    events, on_event = collect()
    try:
        assert run(workflow, on_event) is False
        assert calls == []
        assert [(event.event, event.status) for event in events[-2:]] == [
            ("task_completed", "failed"), ("run_completed", "failed")]
        assert "tasks must be synchronous" in events[-1].error
        assert ("an async generator" if kind == "async_generator" else "an awaitable") in events[-1].error
        if kind == "coroutine":
            assert result.cr_frame is None, "rejected coroutine was not closed"
    finally:
        if kind == "coroutine":
            result.close()


def test_run_ids(monkeypatch):
    workflow = Workflow("test", {}, {})
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    first, on_event = collect()
    run(workflow, on_event)
    second, on_event = collect()
    run(workflow, on_event)
    assert first[0].run_id != second[0].run_id
    monkeypatch.setenv("INVOCATION_ID", "systemd-invocation")
    events, on_event = collect()
    run(workflow, on_event)
    assert events[0].run_id == "systemd-invocation"
    events, on_event = collect()
    run(workflow, on_event, run_id="explicit")
    assert {e.run_id for e in events} == {"explicit"}


def test_tasks_and_child_processes_see_the_run_id(monkeypatch):
    monkeypatch.delenv("FLOE_RUN_ID", raising=False)
    seen = {}

    def task():
        seen["task"] = os.environ.get("FLOE_RUN_ID")
        seen["child"] = subprocess.run(["sh", "-c", 'printf %s "$FLOE_RUN_ID"'],
                                       capture_output=True, text=True, check=True).stdout

    events, on_event = collect()
    run(Workflow("test", {"task": task}, {"task": set()}), on_event, run_id="explicit-id")
    assert seen == {"task": "explicit-id", "child": "explicit-id"}
    assert {e.run_id for e in events} == {"explicit-id"}


@pytest.mark.parametrize("previous", [None, "outer-run"])
@pytest.mark.parametrize("outcome", ["succeeds", "fails", "interrupted"])
def test_run_id_variable_is_restored(monkeypatch, previous, outcome):
    if previous is None:
        monkeypatch.delenv("FLOE_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("FLOE_RUN_ID", previous)

    def task():
        if outcome == "fails":
            raise ValueError("broken")
        if outcome == "interrupted":
            raise KeyboardInterrupt

    workflow = Workflow("test", {"task": task}, {"task": set()})
    events, on_event = collect()
    try:
        run(workflow, on_event, run_id="inner-run")
    except KeyboardInterrupt:
        assert outcome == "interrupted"
    assert os.environ.get("FLOE_RUN_ID") == previous
