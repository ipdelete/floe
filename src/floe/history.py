"""Execution history, reconstructed from the journal.

Each systemd invocation of a workflow's service is one run. Its run ID is the
invocation ID, which journald attaches to everything the run printed and to
systemd's own messages about it. Runs started with `floe run` outside systemd
are not in the journal and therefore not in history.
"""

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import replace
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
    """Runs newest first, with complete records for each selected invocation.

    `since` selects runs with journal activity in that range, not partial runs.
    Discovery streams the journal; only selected invocations' records are kept.
    """
    service, _ = unit_names(workflow)
    if limit is not None and limit < 1:
        raise ValidationError(["limit must be at least 1"])
    invocations = _run_ids(service, journal, since=since)
    # With a time filter, fetch all candidates: their starts may precede the
    # filter, so discovery alone cannot reliably order them by start time.
    if since is None and limit is not None:
        invocations = invocations[:limit]
    groups = _records_for(service, invocations, journal)
    active = _active_invocation(service, sd)
    runs = [_build_run(workflow, inv, records, inv == active) for inv, records in groups.items() if records]
    runs.sort(key=lambda run: run.started, reverse=True)
    return runs if limit is None else runs[:limit]


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
    if run_id is not None and len(run_id) == 32:
        invocations = [run_id]
    else:
        invocations = _run_ids(service, journal, prefix=run_id)
        if run_id is None:
            invocations = invocations[:1]
    if len(invocations) > 1:
        raise NotFoundError(f"run ID {run_id!r} is ambiguous: matches {', '.join(sorted(invocations))}")
    groups = _records_for(service, invocations, journal)
    if not invocations or not groups[invocations[0]]:
        raise NotFoundError(f"no runs of {service} recorded" if run_id is None
                            else f"no run of {service} matches {run_id!r}")
    invocation = invocations[0]
    return invocation, groups[invocation]


def _run_ids(service: str, journal: Journal, *, since: str | None = None,
             prefix: str | None = None) -> list[str]:
    """Index start times, not record adjacency or the last output a run wrote."""
    first: dict[str, datetime] = {}
    started: dict[str, datetime] = {}
    for record in journal.records(service, since=since, reverse=True):
        invocation = _invocation(record)
        if invocation is None or (prefix is not None and not invocation.startswith(prefix)):
            continue
        when = _time(record)
        first[invocation] = min(first.get(invocation, when), when)
        event = _event(record, invocation)
        if event is not None and event.event == "run_started":
            when = _event_time(event)
            started[invocation] = min(started.get(invocation, when), when)
    return sorted(first, key=lambda inv: started.get(inv, first[inv]), reverse=True)


def _records_for(service: str, invocations: list[str], journal: Journal) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {inv: [] for inv in invocations}
    if groups:
        # Avoid oversized command lines for explicitly large history requests.
        selected = tuple(invocations) if len(invocations) <= 128 else None
        for record in journal.records(service, invocations=selected):
            invocation = _invocation(record)
            if invocation in groups:
                groups[invocation].append(record)
    return groups


def _active_invocation(service: str, sd: Systemd) -> str | None:
    props = sd.show(service, "ActiveState", "InvocationID")
    if props.get("ActiveState") not in _RUNNING:
        return None
    return props.get("InvocationID") or None


# --- Record parsing ------------------------------------------------------------

def _invocation(record: dict) -> str | None:
    value = record.get("_SYSTEMD_INVOCATION_ID") or record.get("USER_INVOCATION_ID")
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) else None


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


def _event_time(event: Event) -> datetime:
    # Event.from_dict has already checked that the timestamp has a UTC offset.
    return datetime.fromisoformat(event.timestamp).astimezone(UTC)


def _build_run(workflow: str, invocation: str, records: list[dict], running: bool) -> Run:
    tasks: dict[str, TaskRun] = {}
    started: datetime | None = None
    completed: tuple[Event, datetime] | None = None
    output: list[str] = []
    terminal_time: datetime | None = None
    result: str | None = None
    failed = False
    for record in records:
        if _from_systemd(record):
            if record.get("MESSAGE_ID") in _TERMINAL:
                terminal_time = _time(record)
            result = record.get("UNIT_RESULT") or result
            if (record.get("MESSAGE_ID") in (_JOB_FAILED, _UNIT_FAILED)
                    or record.get("JOB_RESULT") == "failed"
                    or record.get("UNIT_RESULT") not in (None, "", "success")):
                failed = True
            continue
        event = _event(record, invocation)
        if event is None:
            output.append(_message(record))
            continue
        when = _event_time(event)
        if event.event == "run_started":
            started = when
        elif event.event == "task_started" and event.task:
            tasks[event.task] = TaskRun(event.task, "running", started=when)
        elif event.event == "task_completed" and event.task:
            task = tasks.get(event.task) or TaskRun(event.task, "failed")
            tasks[event.task] = replace(task, status=event.status or "failed", finished=when, error=event.error)
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
        for name, task in tasks.items():
            if task.status == "running":
                tasks[name] = replace(task, status="incomplete")
    return Run(workflow=workflow, run_id=invocation, status=status, started=started or first,
               finished=finished, error=error, tasks=tuple(tasks.values()),
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
        # Every line uses journald's receive time, the clock of the order lines are
        # shown in; the event's own timestamp stays available in `event`.
        yield LogLine(when, "event", message, task=event.task, priority=priority, event=event)
        if event.event == "task_completed":
            task = None
