import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("workflow", Path(__file__).parents[1] / "workflow.py")
workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow)


def events(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.parametrize("dag", [
    {"b": {"a"}},
    {"a": set(), "b": {"unknown"}},
    {"a": {"b"}, "b": {"a"}},
])
def test_invalid_graph_executes_nothing(dag, capsys):
    calls = []
    tasks = {name: lambda: calls.append(True) for name in ("a", "b")}
    assert workflow.run_workflow("test", tasks, dag) == 1
    assert calls == []
    recorded = events(capsys)
    assert [event["event"] for event in recorded] == ["run_started", "run_completed"]
    assert recorded[-1]["status"] == "failed"
    assert recorded[-1]["error"]


@pytest.mark.parametrize("bad_name", ["", 123])
def test_invalid_task_names_fail_before_execution(bad_name, capsys):
    calls = []
    assert workflow.run_workflow("test", {bad_name: lambda: calls.append(True)}, {bad_name: set()}) == 1
    assert calls == []
    assert events(capsys)[-1]["status"] == "failed"


def test_noncallable_task_fails_before_any_task_runs(capsys):
    calls = []
    tasks = {"first": lambda: calls.append(True), "broken": None}
    assert workflow.run_workflow("test", tasks, {"first": set(), "broken": {"first"}}) == 1
    assert calls == []
    assert "broken" in events(capsys)[-1]["error"]


def test_failure_stops_dependents_and_independent_tasks(capsys):
    calls = []

    def fail():
        calls.append("fail")
        raise ValueError("broken input")

    tasks = {"fail": fail, "independent": lambda: calls.append("independent"),
             "dependent": lambda: calls.append("dependent")}
    dag = {"fail": set(), "independent": set(), "dependent": {"fail"}}
    assert workflow.run_workflow("test", tasks, dag) == 1
    assert calls == ["fail"]
    recorded = events(capsys)
    assert [e["event"] for e in recorded] == [
        "run_started", "task_started", "task_completed", "run_completed",
    ]
    assert recorded[2]["task"] == "fail"
    assert recorded[2]["status"] == "failed"
    assert recorded[2]["error"] == recorded[3]["error"] == "broken input"


def test_success_events_and_dependency_order(capsys):
    calls = []
    tasks = {name: lambda n=name: calls.append(n) for name in ("b", "a")}
    assert workflow.run_workflow("test", tasks, {"b": {"a"}, "a": set()}) == 0
    assert calls == ["a", "b"]
    recorded = events(capsys)
    assert [e["event"] for e in recorded] == [
        "run_started", "task_started", "task_completed",
        "task_started", "task_completed", "run_completed",
    ]
    assert recorded[-1]["status"] == "succeeded"
    assert len({e["run_id"] for e in recorded}) == 1
    assert all(e["schema"] == 1 and e["workflow"] == "test" and e["timestamp"] for e in recorded)


def test_run_ids_are_per_invocation(monkeypatch, capsys):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    workflow.run_workflow("test", {}, {})
    first = events(capsys)[0]["run_id"]
    workflow.run_workflow("test", {}, {})
    assert events(capsys)[0]["run_id"] != first
    monkeypatch.setenv("INVOCATION_ID", "systemd-invocation")
    workflow.run_workflow("test", {}, {})
    assert events(capsys)[0]["run_id"] == "systemd-invocation"


def test_graph_error_identifies_names(capsys):
    workflow.run_workflow("test", {"missing": lambda: None}, {"extra": set()})
    error = events(capsys)[-1]["error"]
    assert "missing" in error and "extra" in error
    workflow.run_workflow("test", {"a": lambda: None}, {"a": {"unknown"}})
    assert "unknown" in events(capsys)[-1]["error"]
