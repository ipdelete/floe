"""Execute a validated workflow in-process, reporting lifecycle events.

Tenacity provides bounded task-call retries. Events go to
the caller's `on_event` callback; serializing them (e.g. JSON lines for
journald) is the UI's job.
"""

import inspect
import os
import uuid
from collections.abc import Callable
from concurrent.futures import CancelledError
from datetime import UTC, datetime

from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_fixed

from .model import Event, ValidationError, Workflow
from .results import RunResults

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
                       timestamp=datetime.now(UTC).isoformat(),
                       schema=2 if workflow.retries else 1, **fields))

    results = RunResults(workflow.inputs)
    emit("run_started")
    for name in workflow.order:
        emit("task_started", task=name)
        policy = workflow.retries.get(name)
        if policy is None:
            # Keep the original event sequence for unconfigured tasks.
            try:
                result = workflow.tasks[name](**results.arguments(name))
                _check_result(result, name)
                results.complete(name, result)
                del result
            except Exception as exc:
                emit("task_completed", task=name, status="failed", error=str(exc))
                emit("run_completed", status="failed", error=str(exc))
                return False
        else:
            def before_sleep(state):
                emit("retry_scheduled", task=name, attempt=state.attempt_number,
                     max_attempts=policy.max_attempts, delay_seconds=state.next_action.sleep)

            retrying = Retrying(stop=stop_after_attempt(policy.max_attempts),
                                wait=wait_fixed(policy.delay_seconds),
                                retry=retry_if_exception_type(policy.retry_on),
                                before_sleep=before_sleep, retry_error_callback=lambda state: None)
            # Tenacity owns counting, waits, and exhaustion. Its context catches
            # ONLY task-call errors, never callbacks or synchronous validation.
            error = None
            reason = "exhausted"
            for attempt in retrying:
                number = attempt.retry_state.attempt_number
                fields = dict(task=name, attempt=number, max_attempts=policy.max_attempts)
                emit("attempt_started", **fields)
                result = None
                kwargs = results.arguments(name)
                with attempt:
                    result = workflow.tasks[name](**kwargs)
                del kwargs
                outcome = attempt.retry_state.outcome
                error = outcome.exception() if outcome.failed else None
                if error is not None and (not isinstance(error, Exception) or isinstance(error, CancelledError)):
                    raise error
                if error is None:
                    try:
                        _check_result(result, name)
                        results.complete(name, result)
                        del result
                    except Exception as exc:
                        error = exc
                        retryable = False
                    else:
                        emit("attempt_completed", **fields, status="succeeded")
                        break
                else:
                    retryable = isinstance(error, policy.retry_on)
                emit("attempt_completed", **fields, status="failed", error=str(error))
                if not retryable:
                    reason = "non_retryable"
                    break
            if error is not None:
                emit("task_completed", task=name, status="failed", error=str(error), failure_reason=reason)
                emit("run_completed", status="failed", error=str(error))
                return False
        emit("task_completed", task=name, status="succeeded")
    emit("run_completed", status="succeeded")
    return True


def _check_result(result: object, name: str) -> None:
    if inspect.isawaitable(result) or inspect.isasyncgen(result):
        if inspect.iscoroutine(result):
            result.close()
        kind = "an async generator" if inspect.isasyncgen(result) else "an awaitable"
        raise ValidationError([f"task {name!r} returned {kind}; tasks must be synchronous"])
