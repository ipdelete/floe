"""The runner executes a workflow's tasks and reports what happened as events.

Nothing is faked: tasks are plain functions and events are collected in a list.
"""

import floe


def chain(*names, fail=None):
    """A workflow where each task depends on the one before it; returns (workflow, ran)."""
    ran = []

    def task(name):
        def call():
            ran.append(name)
            if name == fail:
                raise RuntimeError(f"{name} broke")
        return call

    # Insert tasks in reverse so only the dag can produce the right order.
    tasks = {name: task(name) for name in reversed(names)}
    dag = {name: {names[i - 1]} if i else set() for i, name in enumerate(names)}
    return floe.Workflow(name="nightly", tasks=tasks, dag=dag), ran


def test_tasks_run_in_dependency_order():
    workflow, ran = chain("fetch", "transform", "publish")

    floe.run(workflow, lambda event: None)

    assert ran == ["fetch", "transform", "publish"]


def test_first_failure_stops_the_run():
    workflow, ran = chain("fetch", "transform", "publish", fail="transform")

    floe.run(workflow, lambda event: None)

    assert ran == ["fetch", "transform"]


def test_successful_run_returns_true():
    workflow, _ = chain("fetch", "publish")

    assert floe.run(workflow, lambda event: None) is True


def test_failed_run_returns_false():
    workflow, _ = chain("fetch", "publish", fail="fetch")

    assert floe.run(workflow, lambda event: None) is False


def test_events_report_each_step_of_a_failed_run():
    workflow, _ = chain("fetch", "transform", "publish", fail="transform")
    events = []

    floe.run(workflow, events.append)

    assert [(e.event, e.task, e.status) for e in events] == [
        ("run_started", None, None),
        ("task_started", "fetch", None),
        ("task_completed", "fetch", "succeeded"),
        ("task_started", "transform", None),
        ("task_completed", "transform", "failed"),
        ("run_completed", None, "failed"),
    ]


def test_failed_run_completes_with_the_task_error():
    workflow, _ = chain("fetch", fail="fetch")
    events = []

    floe.run(workflow, events.append)

    assert (events[-1].event, events[-1].error) == ("run_completed", "fetch broke")


def test_run_id_is_the_invocation_id(monkeypatch):
    invocation = "ff55c14097a84a46bec1b4bd8f85d06b"
    monkeypatch.setenv("INVOCATION_ID", invocation)
    workflow, _ = chain("fetch", "publish")
    events = []

    floe.run(workflow, events.append)

    assert {event.run_id for event in events} == {invocation}
