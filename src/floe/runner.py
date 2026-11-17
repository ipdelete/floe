"""Execute a validated workflow in-process, reporting lifecycle events.

Standard library only: systemd runs this with the system Python. Events go to
the caller's `on_event` callback; serializing them (e.g. JSON lines for
journald) is the UI's job.
"""

import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from .model import Event, Workflow

__all__ = ["default_run_id", "run"]

# Set for the duration of a run so tasks and their child processes can identify it.
_RUN_ID_VARIABLE = "FLOE_RUN_ID"


def default_run_id() -> str:
    """systemd's INVOCATION_ID when running as a service, otherwise a new UUID."""
    return os.environ.get("INVOCATION_ID") or uuid.uuid4().hex


def run(workflow: Workflow, on_event: Callable[[Event], object], *, run_id: str | None = None) -> bool:
    """Run tasks one at a time in dependency order; return whether all succeeded.

    The first task exception stops the run: later tasks, dependent or not, are
    skipped. Validation already happened when the Workflow was constructed.
    While it runs, the environment variable FLOE_RUN_ID holds the run ID; its
    previous value is restored afterwards.
    """
    run_id = run_id or default_run_id()
    previous = os.environ.get(_RUN_ID_VARIABLE)
    os.environ[_RUN_ID_VARIABLE] = run_id
    try:
        return _run(workflow, on_event, run_id)
    finally:
        if previous is None:
            os.environ.pop(_RUN_ID_VARIABLE, None)
        else:
            os.environ[_RUN_ID_VARIABLE] = previous


def _run(workflow: Workflow, on_event: Callable[[Event], object], run_id: str) -> bool:
    def emit(event, **fields) -> None:
        on_event(Event(event=event, workflow=workflow.name, run_id=run_id,
                       timestamp=datetime.now(UTC).isoformat(), **fields))

    emit("run_started")
    for name in workflow.order:
        emit("task_started", task=name)
        try:
            workflow.tasks[name]()
        except Exception as exc:
            emit("task_completed", task=name, status="failed", error=str(exc))
            emit("run_completed", status="failed", error=str(exc))
            return False
        emit("task_completed", task=name, status="succeeded")
    emit("run_completed", status="succeeded")
    return True
