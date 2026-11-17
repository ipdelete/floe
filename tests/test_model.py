import pytest

from floe.model import Event, ValidationError, Workflow


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


def test_event_dict_is_stable_schema():
    event = Event("task_completed", "demo", "r1", "2026-01-01T00:00:00+00:00", task="a", status="succeeded")
    assert event.to_dict() == {
        "schema": 1, "event": "task_completed", "workflow": "demo", "run_id": "r1",
        "timestamp": "2026-01-01T00:00:00+00:00", "task": "a", "status": "succeeded",
    }
    assert list(Event("run_started", "demo", "r1", "t").to_dict()) == [
        "schema", "event", "workflow", "run_id", "timestamp"]
