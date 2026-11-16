"""Data types and errors shared by the library and every UI.

Everything a UI receives from floe is one of these types. They hold data only:
formatting for humans (text, HTML) belongs to the UI, and `to_dict()` gives a
JSON-ready form for machine interfaces.
"""

import difflib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from types import MappingProxyType
from typing import Literal

EVENT_SCHEMA = 1
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,246}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# --- Errors -----------------------------------------------------------------

class FloeError(Exception):
    """Base class for every error floe raises on purpose."""


class ValidationError(FloeError, ValueError):
    """Invalid input. Lists every problem found; `subject` names what was
    invalid when several problems belong together (e.g. "workflow")."""

    def __init__(self, problems: Iterable[str], subject: str | None = None):
        self.problems = tuple(problems)
        self.subject = subject
        super().__init__("; ".join(self.problems))


class LoadError(FloeError):
    """A workflow file could not be read or executed."""


class NotOwnedError(FloeError, PermissionError):
    """Refused to overwrite or delete a foreign, linked, or masked unit."""


class SystemdError(FloeError):
    """A systemctl command failed."""

    def __init__(self, args: list[str], returncode: int, stderr: str):
        self.command = list(args)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(args)} failed ({returncode}): {stderr.strip()}")


class NotFoundError(FloeError, LookupError):
    """No run (or no unique run) matches the requested ID."""


class UninstallError(FloeError):
    """Uninstall did every step it could, but some failed or left state behind."""

    def __init__(self, removed: list[str], errors: list[str]):
        self.removed = list(removed)
        self.errors = list(errors)
        super().__init__("uninstall incomplete:\n  " + "\n  ".join(self.errors))


# --- Validation helpers -----------------------------------------------------

def name_problems(name: object) -> list[str]:
    if isinstance(name, str) and _NAME.fullmatch(name):
        return []
    return [(f"invalid workflow name {name!r}: use 1–247 characters (letters, digits, _, ., -), "
             "starting with a letter or digit")]


def check_name(name: object) -> str:
    if problems := name_problems(name):
        raise ValidationError(problems)
    return name  # type: ignore[return-value]


def schedule_problems(schedule: object) -> list[str]:
    if isinstance(schedule, str) or not isinstance(schedule, Iterable):
        return ["schedule must be a sequence of OnCalendar expressions, not a single string"]
    return [
        f"invalid schedule entry {value!r}"
        for value in schedule
        if not isinstance(value, str) or not value or _CONTROL.search(value)
        or value != value.strip() or value.endswith("\\")
    ]


# --- Workflow ---------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class Workflow:
    """A validated workflow: construction fails with every problem listed.

    `dag` maps each task name to its prerequisites. `source` is the defining
    file, required for deployment. `schedule` may be empty for run-only use.
    """

    name: str
    tasks: Mapping[str, Callable[[], object]]
    dag: Mapping[str, frozenset[str]]
    schedule: tuple[str, ...] = ()
    source: Path | None = None
    order: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        problems = name_problems(self.name) + schedule_problems(self.schedule)
        tasks = dict(self.tasks) if isinstance(self.tasks, Mapping) else None
        dag = dict(self.dag) if isinstance(self.dag, Mapping) else None
        if tasks is None:
            problems.append("tasks must be a mapping of names to callables")
        if dag is None:
            problems.append("dag must be a mapping of task names to prerequisite sets")
        order: tuple[str, ...] = ()
        if tasks is not None and dag is not None:
            dag = {name: frozenset(deps) if isinstance(deps, Iterable) and not isinstance(deps, str) else deps
                   for name, deps in dag.items()}
            problems += _graph_problems(tasks, dag)
            if not problems:
                try:
                    order = tuple(TopologicalSorter(dag).static_order())
                except CycleError as exc:
                    problems.append(f"dependency cycle: {' -> '.join(map(str, exc.args[1]))}")
        if problems:
            raise ValidationError(problems, subject="workflow")
        object.__setattr__(self, "tasks", MappingProxyType(tasks))
        object.__setattr__(self, "dag", MappingProxyType(dag))
        object.__setattr__(self, "schedule", tuple(self.schedule))
        object.__setattr__(self, "source", None if self.source is None else Path(self.source))
        object.__setattr__(self, "order", order)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "schedule": list(self.schedule),
            "source": None if self.source is None else str(self.source),
            "order": list(self.order),
            "dag": {name: sorted(deps) for name, deps in self.dag.items()},
        }


def _graph_problems(tasks: dict, dag: dict) -> list[str]:
    problems = []
    for name, task in tasks.items():
        if not isinstance(name, str) or not name:
            problems.append(f"task names must be nonempty strings: {name!r}")
        elif not callable(task):
            problems.append(f"task {name!r} is not callable")
    for name, deps in dag.items():
        if not isinstance(deps, frozenset):
            problems.append(f"prerequisites of {name!r} must be a set of task names")
    missing = sorted(map(repr, tasks.keys() - dag.keys()))
    extra = sorted(map(repr, dag.keys() - tasks.keys()))
    if missing:
        problems.append(f"tasks missing from dag: {', '.join(missing)}")
    if extra:
        problems.append(f"dag entries without a task: {', '.join(extra)}")
    unknown = set()
    for deps in dag.values():
        if isinstance(deps, frozenset):
            unknown |= deps - tasks.keys()
    if unknown:
        problems.append(f"unknown prerequisites: {', '.join(sorted(map(repr, unknown)))}")
    return problems


# --- Run events ---------------------------------------------------------------

EventKind = Literal["run_started", "task_started", "task_completed", "run_completed"]
Status = Literal["succeeded", "failed"]


@dataclass(frozen=True)
class Event:
    """One lifecycle event of a run. `to_dict()` is the stable JSON schema."""

    event: EventKind
    workflow: str
    run_id: str
    timestamp: str
    task: str | None = None
    status: Status | None = None
    error: str | None = None
    schema: int = EVENT_SCHEMA

    @classmethod
    def from_dict(cls, data: object) -> Event | None:
        """Parse a JSON event, or return None if it isn't one of this schema."""
        if not isinstance(data, dict) or data.get("schema") != EVENT_SCHEMA:
            return None
        if data.get("event") not in ("run_started", "task_started", "task_completed", "run_completed"):
            return None
        fields = {key: data.get(key) for key in ("workflow", "run_id", "timestamp", "task", "status", "error")}
        if not all(isinstance(fields[key], str) for key in ("workflow", "run_id", "timestamp")):
            return None
        if any(fields[key] is not None and not isinstance(fields[key], str) for key in ("task", "status", "error")):
            return None
        return cls(event=data["event"], **fields)

    def to_dict(self) -> dict:
        data = {"schema": self.schema, "event": self.event, "workflow": self.workflow,
                "run_id": self.run_id, "timestamp": self.timestamp}
        for key in ("task", "status", "error"):
            if (value := getattr(self, key)) is not None:
                data[key] = value
        return data


# --- Deployment -------------------------------------------------------------

Action = Literal["create", "update", "unchanged", "adopt", "conflict"]


@dataclass(frozen=True)
class UnitChange:
    """What installing would do to one unit file."""

    name: str
    path: Path
    action: Action
    text: str
    current: str | None = None
    reason: str | None = None

    def diff(self) -> str:
        if self.current is None or self.action == "unchanged":
            return ""
        return "".join(difflib.unified_diff(
            self.current.splitlines(keepends=True), self.text.splitlines(keepends=True),
            fromfile=f"{self.path} (installed)", tofile=f"{self.path} (new)",
        ))

    def to_dict(self) -> dict:
        return {"name": self.name, "path": str(self.path), "action": self.action,
                "text": self.text, "reason": self.reason, "diff": self.diff()}


@dataclass(frozen=True)
class InstallPlan:
    """Inspect with UIs, then pass to `deploy.apply`. Nothing is written yet."""

    workflow: str
    timer: str
    changes: tuple[UnitChange, ...]

    @property
    def conflicts(self) -> tuple[UnitChange, ...]:
        return tuple(c for c in self.changes if c.action == "conflict")

    @property
    def writes(self) -> tuple[UnitChange, ...]:
        return tuple(c for c in self.changes if c.action in ("create", "update", "adopt"))

    def to_dict(self) -> dict:
        return {"workflow": self.workflow, "changes": [c.to_dict() for c in self.changes]}


@dataclass(frozen=True)
class UnitState:
    """systemd properties of one unit, as reported by `systemctl show`."""

    name: str
    properties: Mapping[str, str]

    def __getitem__(self, key: str) -> str:
        return self.properties.get(key, "")

    def to_dict(self) -> dict:
        return {"name": self.name, "properties": dict(self.properties)}


# --- History ----------------------------------------------------------------

RunStatus = Literal["running", "succeeded", "failed", "incomplete"]


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _seconds(start: datetime | None, end: datetime | None) -> timedelta | None:
    return None if start is None or end is None else end - start


@dataclass(frozen=True)
class TaskRun:
    """One task within a recorded run. `incomplete`: the run ended mid-task."""

    name: str
    status: RunStatus
    started: datetime | None = None
    finished: datetime | None = None
    error: str | None = None

    @property
    def duration(self) -> timedelta | None:
        return _seconds(self.started, self.finished)

    def to_dict(self) -> dict:
        duration = self.duration
        return {"name": self.name, "status": self.status, "started": _iso(self.started),
                "finished": _iso(self.finished), "error": self.error,
                "duration": None if duration is None else duration.total_seconds()}


@dataclass(frozen=True)
class Run:
    """One execution under systemd, reconstructed from the journal.

    status:
      succeeded/failed  from the run's run_completed event; a run that failed
                        before run_started (e.g. an invalid definition) is
                        failed with no tasks and the error output as `error`
      running           no run_completed yet and the service is still active
      incomplete        started, but ended without run_completed (killed,
                        timed out, crashed) or its records are missing
    `result` is systemd's unit result when it reported one (e.g. "exit-code",
    "timeout", "signal").
    """

    workflow: str
    run_id: str
    status: RunStatus
    started: datetime | None
    finished: datetime | None = None
    error: str | None = None
    tasks: tuple[TaskRun, ...] = ()
    result: str | None = None

    @property
    def duration(self) -> timedelta | None:
        return _seconds(self.started, self.finished)

    @property
    def failed_task(self) -> TaskRun | None:
        return next((t for t in self.tasks if t.status in ("failed", "incomplete")), None)

    def to_dict(self) -> dict:
        duration = self.duration
        return {"workflow": self.workflow, "run_id": self.run_id, "status": self.status,
                "started": _iso(self.started), "finished": _iso(self.finished),
                "duration": None if duration is None else duration.total_seconds(),
                "error": self.error, "result": self.result, "tasks": [t.to_dict() for t in self.tasks]}


LogKind = Literal["event", "output", "systemd"]


@dataclass(frozen=True)
class LogLine:
    """One journal line of a run: a floe event, task output, or a systemd message.
    `task` is the task that was running when output was written."""

    timestamp: datetime
    kind: LogKind
    message: str
    task: str | None = None
    priority: int = 6
    event: Event | None = None

    def to_dict(self) -> dict:
        return {"timestamp": _iso(self.timestamp), "kind": self.kind, "message": self.message,
                "task": self.task, "priority": self.priority,
                "event": None if self.event is None else self.event.to_dict()}


@dataclass(frozen=True)
class RunLogs:
    run: Run
    lines: tuple[LogLine, ...]

    def to_dict(self) -> dict:
        return {"run": self.run.to_dict(), "lines": [line.to_dict() for line in self.lines]}
