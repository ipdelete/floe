import json
import os
import sys
import uuid
from datetime import datetime, timezone
from graphlib import TopologicalSorter
from pathlib import Path

WORKFLOW = "mydag"
SCHEDULE = ["hourly"]  # OnCalendar= expressions, see systemd.time(7)

RUN_ID = os.environ.get("INVOCATION_ID") or uuid.uuid4().hex


def emit(event, **fields):
    print(json.dumps({
        "schema": 1,
        "event": event,
        "workflow": WORKFLOW,
        "run_id": RUN_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **fields,
    }), flush=True)


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


def main():
    emit("run_started")
    try:
        # Validate the entire graph before executing any tasks.
        nodes = set(dag).union(*dag.values())
        if nodes != set(tasks):
            raise ValueError("DAG nodes and task functions must match")
        order = list(TopologicalSorter(dag).static_order())

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
    sys.exit(cli(Deployment(WORKFLOW, Path(__file__), SCHEDULE), sys.argv[1:]))
