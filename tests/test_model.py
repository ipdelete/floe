import json
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from floe.model import Event, InstalledWorkflow, UnitState, ValidationError, Workflow, WorkflowSummary


def noop():
    pass


def test_valid_workflow_is_frozen_and_ordered():
    tasks = {"b": noop, "a": noop}
    dag = {"b": {"a"}, "a": set()}
    workflow = Workflow("demo", tasks, dag, ["hourly"])
    assert workflow.order == ("a", "b")
    assert workflow.schedule == ("hourly",)
    assert workflow.dag == {"a": frozenset(), "b": frozenset({"a"})}
    tasks["c"] = noop  # snapshot: later changes to the inputs don't leak in
    assert set(workflow.tasks) == {"a", "b"}
    with pytest.raises(TypeError):
        workflow.tasks["c"] = noop
    assert workflow.to_dict()["order"] == ["a", "b"]


@pytest.mark.parametrize("schedule_type", [list, tuple, iter])
def test_schedule_is_snapshotted_without_consuming_it_twice(schedule_type):
    workflow = Workflow("demo", {}, {}, schedule_type(["hourly", "daily"]))
    assert workflow.schedule == ("hourly", "daily")


def test_prerequisite_iterators_are_normalized_once():
    workflow = Workflow("demo", {"a": noop, "b": noop}, {"a": iter(["b"]), "b": set()})
    assert workflow.dag == {"a": frozenset({"b"}), "b": frozenset()}
    assert workflow.order == ("b", "a")


@pytest.mark.parametrize("tasks,dag,expected", [
    ({"a": noop, "b": noop}, {"b": {"a"}}, ["missing from dag: 'a'"]),
    ({"a": noop}, {"a": set(), "x": set()}, ["without a task: 'x'"]),
    ({"a": noop}, {"a": {"unknown"}}, ["unknown prerequisites: 'unknown'"]),
    ({"a": noop, "b": noop}, {"a": {"b"}, "b": {"a"}}, ["dependency cycle"]),
    ({"": noop}, {"": set()}, ["nonempty strings"]),
    ({123: noop}, {123: set()}, ["nonempty strings"]),
    ({"a": None}, {"a": set()}, ["'a' is not callable"]),
    ({"a": noop}, {"a": "b"}, ["must be a set"]),
    ({"a": noop}, {"a": [["b"]]}, ["nonempty strings"]),
    ({"a": noop}, {"a": [{"b": 1}]}, ["nonempty strings"]),
    ({"a": noop}, {"a": [set()]}, ["nonempty strings"]),
    ({"a": noop}, {"a": [123, ""]}, ["nonempty strings"]),
    ("tasks", {}, ["tasks must be a mapping"]),
    ({"a": noop}, [], ["dag must be a mapping"]),
])
def test_invalid_graphs(tasks, dag, expected):
    with pytest.raises(ValidationError) as caught:
        Workflow("demo", tasks, dag)
    for text in expected:
        assert any(text in problem for problem in caught.value.problems), caught.value.problems


def test_bad_prerequisites_do_not_hide_other_validation_errors():
    with pytest.raises(ValidationError) as caught:
        Workflow("bad/name", {"a": None}, {"a": [["b"], "missing"]}, "hourly")
    problems = " | ".join(caught.value.problems)
    for text in ("workflow name", "schedule", "not callable", "nonempty strings", "unknown prerequisites"):
        assert text in problems
    assert isinstance(caught.value, ValueError)


@pytest.mark.parametrize("name", ["", "../escape", "-option", "space name", "x@", "x" * 248, None])
def test_invalid_names(name):
    with pytest.raises(ValidationError, match="workflow name"):
        Workflow(name, {}, {})


@pytest.mark.parametrize("schedule", [[""], ["hourly\nUnit=x"], [" hourly"], ["daily\\"], "hourly", [1]])
def test_invalid_schedules(schedule):
    with pytest.raises(ValidationError, match="schedule"):
        Workflow("demo", {}, {}, schedule)


def test_invalid_schedule_iterator_reports_all_errors():
    with pytest.raises(ValidationError) as caught:
        Workflow("bad/name", {"a": None}, {"a": set()}, iter(["hourly", None]))
    problems = " | ".join(caught.value.problems)
    for text in ("workflow name", "not callable", "invalid schedule entry None"):
        assert text in problems


@pytest.mark.parametrize("kind,task,status", [
    ("run_started", None, None),
    ("task_started", "fetch", None),
    ("task_completed", "fetch", "succeeded"),
    ("run_completed", None, "failed"),
])
def test_event_schema_round_trip(kind, task, status):
    event = Event(kind, "demo", "a" * 32, "2026-01-01T00:00:00+00:00", task=task, status=status)
    assert Event.from_dict(event.to_dict()) == event


@pytest.mark.parametrize("changes", [
    {"schema": True}, {"schema": 1.0}, {"schema": 2},
    {"event": "unknown"}, {"event": []},
    {"workflow": ""}, {"run_id": ""}, {"timestamp": ""},
    {"timestamp": "not a date"}, {"timestamp": "2026-01-01T00:00:00"},
    {"timestamp": "0001-01-01T00:00:00+01:00"},
    {"event": "task_started"}, {"event": "task_started", "task": ""},
    {"task": "unexpected"}, {"status": "not-a-status"},
    {"event": "run_completed"}, {"event": "task_completed", "task": "fetch"},
    {"error": "unexpected"},
])
def test_event_schema_rejects_malformed_records(changes):
    data = Event("run_started", "demo", "a" * 32, "2026-01-01T00:00:00+00:00").to_dict()
    assert Event.from_dict({**data, **changes}) is None


def test_event_dict_is_stable_schema():
    event = Event("task_completed", "demo", "r1", "2026-01-01T00:00:00+00:00", task="a", status="succeeded")
    assert event.to_dict() == {
        "schema": 1, "event": "task_completed", "workflow": "demo", "run_id": "r1",
        "timestamp": "2026-01-01T00:00:00+00:00", "task": "a", "status": "succeeded",
    }
    assert list(Event("run_started", "demo", "r1", "t").to_dict()) == [
        "schema", "event", "workflow", "run_id", "timestamp"]


def test_timer_state_json_is_additive_and_legacy_positional_arguments_keep_their_meaning():
    installed = InstalledWorkflow("demo", None, None, None)
    when = datetime(2026, 11, 17, tzinfo=UTC)
    # All eight pre-existing arguments stay in their original positions.
    legacy = WorkflowSummary(installed, ("hourly",), False, True, when, None, None, ("problem",))
    expected = {
        "name": "demo", "script": None, "service": None, "timer": None,
        "schedule": ["hourly"], "enabled": False, "active": True,
        "next_run": "2026-11-17T00:00:00+00:00", "last_trigger": None, "last_run": None,
        "problems": ["problem"], "timer_state": None,
    }
    assert json.loads(json.dumps(legacy.to_dict())) == expected
    state = UnitState("demo.timer", MappingProxyType({
        "ActiveState": "active", "UnitFileState": "enabled-runtime", "TimersCalendar": "hourly"}))
    summary = replace(legacy, timer_state=state)
    assert json.loads(json.dumps(summary.to_dict())) == {
        **expected, "timer_state": {
            "name": "demo.timer", "properties": {
                "ActiveState": "active", "UnitFileState": "enabled-runtime", "TimersCalendar": "hourly"},
        },
    }
