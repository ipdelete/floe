import json
import os
import sys
import uuid
from datetime import UTC, datetime
from graphlib import TopologicalSorter
from pathlib import Path

WORKFLOW = "mydag"
SCHEDULE = ["hourly"]  # OnCalendar= expressions, see systemd.time(7)


def fetch():
    print("Fetching data", file=sys.stderr)


def transform():
    print("Transforming data", file=sys.stderr)


def publish():
    print("Publishing data", file=sys.stderr)


tasks = {
    "fetch": fetch,
    "transform": transform,
    "publish": publish,
}

dag = {
    "fetch": set(),
    "transform": {"fetch"},
    "publish": {"transform"},
}


def run_workflow(workflow, tasks, dependencies):
    """Validate and execute a DAG sequentially, emitting JSON lifecycle events."""
    run_id = os.environ.get("INVOCATION_ID") or uuid.uuid4().hex

    def emit(event, **fields):
        print(json.dumps({
            "schema": 1,
            "event": event,
            "workflow": workflow,
            "run_id": run_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **fields,
        }), flush=True)

    emit("run_started")
    try:
        for name, task in tasks.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"Task names must be nonempty strings: {name!r}")
            if not callable(task):
                raise ValueError(f"Task {name!r} is not callable")
        # Materialize the entire order so cycles cannot cause partial execution.
        if tasks.keys() != dependencies.keys():
            missing = sorted(tasks.keys() - dependencies.keys())
            extra = sorted(dependencies.keys() - tasks.keys())
            raise ValueError(f"DAG entries: missing={missing}, extra={extra}")
        unknown = set().union(*dependencies.values()) - tasks.keys()
        if unknown:
            raise ValueError(f"Unknown prerequisites: {sorted(unknown)}")
        order = list(TopologicalSorter(dependencies).static_order())

        for name in order:
            emit("task_started", task=name)
            try:
                tasks[name]()
            except Exception as exc:
                emit("task_completed", task=name, status="failed",
                     error=str(exc))
                raise
            emit("task_completed", task=name, status="succeeded")

    except Exception as exc:
        emit("run_completed", status="failed", error=str(exc))
        return 1

    emit("run_completed", status="succeeded")
    return 0


def main():
    return run_workflow(WORKFLOW, tasks, dag)


if __name__ == "__main__":
    # `run` (or no argument) executes the workflow: this is what systemd calls,
    # with /usr/bin/python3 and only the standard library.
    if sys.argv[1:] in ([], ["run"]):
        sys.exit(main())
    # Everything else manages the deployment and needs the floe package:
    #   uv run python workflow.py install|uninstall|start|status
    try:
        from floe.deploy import Deployment, cli
    except ImportError:
        sys.exit(f"'{sys.argv[1]}' needs the floe package; run: uv run python workflow.py {' '.join(sys.argv[1:])}")
    try:
        deployment = Deployment(WORKFLOW, Path(__file__), SCHEDULE)
    except (ValueError, TypeError) as exc:
        sys.exit(f"error: {exc}")
    sys.exit(cli(deployment, sys.argv[1:]))
