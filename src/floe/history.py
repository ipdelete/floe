"""Execution history, reconstructed from the journal.

Each systemd invocation of a workflow's service is one run. Its run ID is the
invocation ID, which journald attaches to everything the run printed and to
systemd's own messages about it. Runs started with `floe run` outside systemd
are not in the journal and therefore not in history.
"""

import json
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta

from .deploy import unit_names
from .journal import Journal
from .model import Event, LogLine, NotFoundError, Run, RunLogs, TaskRun, ValidationError
from .systemd import Systemd

__all__ = ["get_run", "list_runs", "run_logs"]

# systemd catalog message IDs (see `journalctl --list-catalog`) that end an invocation.
_JOB_DONE = "39f53479d3a045ac8e11786248231fbf"      # Finished/Stopped (JOB_RESULT says how)
_JOB_FAILED = "be02cf6855d2428ba40df7e9d022f03d"    # Failed to start
_UNIT_FAILED = "d9b373ed55a64feb8242e02dbe79a49c"   # Failed with result '...'
_UNIT_SUCCESS = "7ad2d189f7e94e70a38c781354912448"  # Deactivated successfully
_TERMINAL = {_JOB_DONE, _JOB_FAILED, _UNIT_FAILED, _UNIT_SUCCESS}
_RUNNING = ("active", "activating", "deactivating", "reloading", "refreshing")
_RUN_ID = re.compile(r"[0-9a-f]{1,32}")


def list_runs(workflow: str, journal: Journal, sd: Systemd, *,
              since: str | None = None, limit: int | None = 20) -> list[Run]:
    """Recorded runs of an installed workflow, newest first."""
    service, _ = unit_names(workflow)
    if limit is not None and limit < 1:
        raise ValidationError(["limit must be at least 1"])
    groups: dict[str, list[dict]] = {}
    # Newest first; runs of a oneshot service don't overlap, so once a new
    # invocation appears after `limit` of them, those are complete.
    for record in journal.records(service, since=since, reverse=True):
        invocation = _invocation(record)
        if invocation is None:
            continue
        if invocation not in groups:
            if limit is not None and len(groups) == limit:
                break
            groups[invocation] = []
        groups[invocation].append(record)
    active = _active_invocation(service, sd)
    return [_build_run(workflow, inv, records[::-1], inv == active) for inv, records in groups.items()]


def get_run(workflow: str, run_id: str | None, journal: Journal, sd: Systemd) -> Run:
    """One run by full ID or unique prefix; the latest run if run_id is None."""
    return run_logs(workflow, run_id, journal, sd).run


def run_logs(workflow: str, run_id: str | None, journal: Journal, sd: Systemd) -> RunLogs:
    """A run with every journal line it produced, in order."""
    service, _ = unit_names(workflow)
    invocation, records = _find(service, run_id, journal)
    run = _build_run(workflow, invocation, records, invocation == _active_invocation(service, sd))
    return RunLogs(run, tuple(_log_lines(invocation, records)))


def _find(service: str, run_id: str | None, journal: Journal) -> tuple[str, list[dict]]:
    if run_id is not None and not _RUN_ID.fullmatch(run_id):
        raise ValidationError([f"invalid run ID {run_id!r}: expected up to 32 lowercase hex digits"])
    groups: dict[str, list[dict]] = {}
    for record in journal.records(service, reverse=True):
        invocation = _invocation(record)
        if invocation is None:
            continue
        if run_id is None:
            if groups and invocation not in groups:
                break  # the latest run is complete
        elif not invocation.startswith(run_id):
            continue
        groups.setdefault(invocation, []).append(record)
    if not groups:
        raise NotFoundError(f"no runs of {service} recorded" if run_id is None
                            else f"no run of {service} matches {run_id!r}")
    if len(groups) > 1:
        raise NotFoundError(f"run ID {run_id!r} is ambiguous: matches {', '.join(sorted(groups))}")
    [(invocation, records)] = groups.items()
    return invocation, records[::-1]


def _active_invocation(service: str, sd: Systemd) -> str | None:
    props = sd.show(service, "ActiveState", "InvocationID")
    return props.get("InvocationID") or None if props.get("ActiveState") in _RUNNING else None


# --- Record parsing ------------------------------------------------------------

def _invocation(record: dict) -> str | None:
    return record.get("_SYSTEMD_INVOCATION_ID") or record.get("USER_INVOCATION_ID") or None


def _from_systemd(record: dict) -> bool:
    """systemd's messages about the unit, as opposed to the run's own output."""
    return "_SYSTEMD_INVOCATION_ID" not in record and "USER_INVOCATION_ID" in record


def _message(record: dict) -> str:
    value = record.get("MESSAGE", "")
    if isinstance(value, list):  # journald encodes non-UTF-8 messages as byte arrays
        value = bytes(value).decode(errors="replace")
    return value if isinstance(value, str) else str(value)


def _time(record: dict) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(record["__REALTIME_TIMESTAMP"]))


def _event(record: dict, invocation: str) -> Event | None:
    message = _message(record)
    if _from_systemd(record) or not message.startswith("{"):
        return None
    try:
        event = Event.from_dict(json.loads(message))
    except ValueError:
        return None
    # Only events this invocation emitted about itself count.
    return event if event is not None and event.run_id == invocation else None


def _event_time(event: Event, fallback: datetime) -> datetime:
    try:
        return datetime.fromisoformat(event.timestamp)
    except ValueError:
        return fallback


def _build_run(workflow: str, invocation: str, records: list[dict], running: bool) -> Run:
    tasks: dict[str, dict] = {}
    started = completed = None
    output: list[str] = []
    terminal_time = result = None
    failed = False
    for record in records:
        if _from_systemd(record):
            if record.get("MESSAGE_ID") in _TERMINAL:
                terminal_time = _time(record)
            result = record.get("UNIT_RESULT") or result
            failed |= (record.get("MESSAGE_ID") in (_JOB_FAILED, _UNIT_FAILED)
                       or record.get("JOB_RESULT") == "failed"
                       or record.get("UNIT_RESULT") not in (None, "", "success"))
            continue
        event = _event(record, invocation)
        if event is None:
            output.append(_message(record))
            continue
        when = _event_time(event, _time(record))
        if event.event == "run_started":
            started = when
        elif event.event == "task_started" and event.task:
            tasks[event.task] = {"name": event.task, "status": "running", "started": when}
        elif event.event == "task_completed" and event.task:
            task = tasks.setdefault(event.task, {"name": event.task})
            task.update(status=event.status or "failed", finished=when, error=event.error)
        elif event.event == "run_completed":
            completed = (event, when)

    first = _time(records[0]) if records else None
    if completed:
        event, finished = completed
        status, error = event.status or "failed", event.error
    elif running and terminal_time is None:
        status, finished, error = "running", None, None
    elif started:
        status, finished = "incomplete", terminal_time
        error = "run ended without completing" + (f" (systemd result: {result})" if result else "")
    elif terminal_time is not None and failed and not tasks:
        status, finished, error = "failed", terminal_time, _error_output(output, result)
    else:
        status, finished, error = "incomplete", terminal_time, "no completion recorded"
    if status != "running":
        for task in tasks.values():
            if task["status"] == "running":
                task["status"] = "incomplete"
    return Run(workflow=workflow, run_id=invocation, status=status, started=started or first,
               finished=finished, error=error, tasks=tuple(TaskRun(**task) for task in tasks.values()),
               result=result)


def _error_output(output: list[str], result: str | None) -> str | None:
    """For runs that failed before starting: the final error message printed."""
    lines = [line for line in output if line.strip()]
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("error:"):  # as printed by the floe command
            return "\n".join([lines[index].removeprefix("error:").strip(), *lines[index + 1:]])
    if lines:
        return lines[-1]
    return f"systemd result: {result}" if result else None


def _log_lines(invocation: str, records: Iterable[dict]) -> Iterator[LogLine]:
    task = None
    for record in records:
        when, message = _time(record), _message(record)
        priority = int(record.get("PRIORITY", 6))
        if _from_systemd(record):
            yield LogLine(when, "systemd", message, priority=priority)
            continue
        event = _event(record, invocation)
        if event is None:
            yield LogLine(when, "output", message, task=task, priority=priority)
            continue
        if event.event == "task_started":
            task = event.task
        yield LogLine(_event_time(event, when), "event", message, task=event.task, priority=priority, event=event)
        if event.event == "task_completed":
            task = None
