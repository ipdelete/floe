"""Data types and errors shared by the library and every UI.

Everything a UI receives from floe is one of these types. They hold data only:
formatting for humans (text, HTML) belongs to the UI, and `to_dict()` gives a
JSON-ready form for machine interfaces.
"""

import difflib
import inspect
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from types import FunctionType, MappingProxyType, MethodType
from typing import Literal

EVENT_SCHEMA = 2
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
    """A requested workflow or run does not exist, or a run ID is ambiguous."""


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


def normalize_schedule(schedule: object) -> tuple[tuple[str, ...], list[str]]:
    """Snapshot valid expressions in one pass, collecting all invalid entries."""
    if isinstance(schedule, str) or not isinstance(schedule, Iterable):
        return (), ["schedule must be a sequence of OnCalendar expressions, not a single string"]
    values: list[str] = []
    problems: list[str] = []
    for value in schedule:
        if (not isinstance(value, str) or not value or _CONTROL.search(value)
                or value != value.strip() or value.endswith("\\")):
            problems.append(f"invalid schedule entry {value!r}")
        else:
            values.append(value)
    return tuple(values), problems


def _normalize_dag(dag: object) -> tuple[dict[str, frozenset[str]] | None, list[str]]:
    """Snapshot prerequisites without trying to hash invalid task names."""
    if not isinstance(dag, Mapping):
        return None, ["dag must be a mapping of task names to prerequisite sets"]
    normalized: dict[str, frozenset[str]] = {}
    problems: list[str] = []
    for name, deps in dict(dag).items():
        valid: set[str] = set()
        if isinstance(deps, str) or not isinstance(deps, Iterable):
            problems.append(f"prerequisites of {name!r} must be a set of task names")
        else:
            for dep in deps:
                if not isinstance(dep, str) or not dep:
                    problems.append(f"prerequisites of {name!r} must be nonempty strings: {dep!r}")
                else:
                    valid.add(dep)
        normalized[name] = frozenset(valid)
    return normalized, problems


def _normalize_inputs(inputs: object, tasks: dict | None, dag: dict[str, frozenset[str]] | None,
                      problems: list[str]) -> dict[str, Mapping[str, str]]:
    """Snapshot bindings and add their edges to the private normalized DAG."""
    if not isinstance(inputs, Mapping):
        problems.append("inputs must be a mapping of consumers to keyword bindings")
        return {}
    normalized = {}
    for consumer, bindings in dict(inputs).items():
        if tasks is not None and consumer not in tasks:
            problems.append(f"inputs for unknown task {consumer!r}")
        if not isinstance(bindings, Mapping):
            problems.append(f"inputs for {consumer!r} must be a mapping")
            continue
        keywords = {}
        for keyword, producer in dict(bindings).items():
            if not isinstance(keyword, str) or not keyword:
                problems.append(f"input key for {consumer!r} must be a nonempty string")
                continue
            if not isinstance(producer, str) or not producer or (tasks is not None and producer not in tasks):
                problems.append(f"invalid input producer {producer!r} for {consumer!r}.{keyword}")
                continue
            keywords[keyword] = producer
        if keywords and tasks is not None and callable(tasks.get(consumer)):
            try:
                inspect.signature(tasks[consumer]).bind(**keywords)
            except (TypeError, ValueError) as exc:
                problems.append(f"invalid input signature for {consumer!r}: {exc}")
        if dag is not None and consumer in dag:
            dag[consumer] = dag[consumer].union(keywords.values())
        normalized[consumer] = MappingProxyType(keywords)
    return normalized


def _valid_delay(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


# --- Workflow ---------------------------------------------------------------

_PARALLEL_MARK = object()


def parallel[F](task: F) -> F:
    """Mark a Python task function as eligible for overlap; never schedule or wrap it."""
    if not isinstance(task, FunctionType):
        raise ValidationError(["@parallel must decorate a Python function"])
    task.__floe_parallel__ = _PARALLEL_MARK
    return task


def _parallel_eligible(task: Callable) -> bool:
    if isinstance(task, MethodType):
        task = task.__func__
    return isinstance(task, FunctionType) and vars(task).get('__floe_parallel__') is _PARALLEL_MARK


@dataclass(frozen=True)
class RetryPolicy:
    """Finite per-task retries; retry_on must explicitly name ordinary exceptions."""

    max_attempts: int
    delay_seconds: float
    retry_on: tuple[type[Exception], ...]

    def __post_init__(self) -> None:
        if isinstance(self.retry_on, Iterable) and not isinstance(self.retry_on, (str, bytes)):
            object.__setattr__(self, "retry_on", tuple(self.retry_on))

    def problems(self) -> list[str]:
        problems = []
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            problems.append("max_attempts must be a positive integer")
        if not _valid_delay(self.delay_seconds):
            problems.append("delay_seconds must be finite and nonnegative")
        if (not isinstance(self.retry_on, tuple) or not self.retry_on
                or any(not isinstance(cls, type) or not issubclass(cls, Exception)
                       for cls in self.retry_on)):
            problems.append("retry_on must be a nonempty sequence of Exception subclasses")
        return problems

    def to_dict(self) -> dict:
        return {"max_attempts": self.max_attempts, "delay_seconds": self.delay_seconds,
                "retry_on": [f"{cls.__module__}.{cls.__qualname__}" for cls in self.retry_on]}


@dataclass(frozen=True, eq=False)
class Workflow:
    """A validated workflow: construction fails with every problem listed.

    `dag` maps each task name to its prerequisites. `source` is the defining
    file, required for deployment. `schedule` may be empty for run-only use.
    """

    name: str
    tasks: Mapping[str, Callable[..., object]]
    dag: Mapping[str, frozenset[str]]
    schedule: tuple[str, ...] = ()
    source: Path | None = None
    retries: Mapping[str, RetryPolicy] = field(default_factory=dict)
    inputs: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    order: tuple[str, ...] = field(init=False)
    parallel_tasks: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        schedule, schedule_errors = normalize_schedule(self.schedule)
        tasks = dict(self.tasks) if isinstance(self.tasks, Mapping) else None
        dag, dag_errors = _normalize_dag(self.dag)
        problems = name_problems(self.name) + schedule_errors
        retries = dict(self.retries) if isinstance(self.retries, Mapping) else {}
        if not isinstance(self.retries, Mapping):
            problems.append("retries must be a mapping of task names to RetryPolicy")
        for name, policy in retries.items():
            if tasks is not None and name not in tasks:
                problems.append(f"retry policy for unknown task {name!r}")
            if not isinstance(policy, RetryPolicy):
                problems.append(f"retry policy for {name!r} must be a RetryPolicy")
            else:
                problems.extend(f"retry policy for {name!r}: {problem}" for problem in policy.problems())
        if tasks is None:
            problems.append("tasks must be a mapping of names to callables")
        inputs = _normalize_inputs(self.inputs, tasks, dag, problems)
        problems += dag_errors
        order: tuple[str, ...] = ()
        if tasks is not None and dag is not None:
            graph_errors = _graph_problems(tasks, dag)
            problems += graph_errors
            if not graph_errors and not dag_errors:
                try:
                    order = tuple(TopologicalSorter(dag).static_order())
                except CycleError as exc:
                    problems.append(f"dependency cycle: {' -> '.join(map(str, exc.args[1]))}")
        if problems:
            raise ValidationError(problems, subject="workflow")
        object.__setattr__(self, "inputs", MappingProxyType(inputs))
        object.__setattr__(self, "retries", MappingProxyType(retries))
        object.__setattr__(self, "tasks", MappingProxyType(tasks))
        object.__setattr__(self, "dag", MappingProxyType(dag))
        object.__setattr__(self, "schedule", schedule)
        object.__setattr__(self, "source", None if self.source is None else Path(self.source))
        object.__setattr__(self, "order", order)
        eligible = frozenset(name for name, task in tasks.items() if _parallel_eligible(task))
        object.__setattr__(self, "parallel_tasks", eligible)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            **({"parallel_tasks": sorted(self.parallel_tasks)} if self.parallel_tasks else {}),
            "schedule": list(self.schedule),
            "source": None if self.source is None else str(self.source),
            "order": list(self.order),
            "dag": {name: sorted(deps) for name, deps in self.dag.items()},
            "retries": {name: policy.to_dict() for name, policy in self.retries.items()},
            **({"inputs": {name: dict(bindings) for name, bindings in self.inputs.items()}} if self.inputs else {}),
        }


def _is_async_task(task: object) -> bool:
    for candidate in (task, getattr(task, "__call__", None)):
        if inspect.iscoroutinefunction(candidate) or inspect.isasyncgenfunction(candidate):
            return True
    return False


def _graph_problems(tasks: dict, dag: dict[str, frozenset[str]]) -> list[str]:
    problems = []
    for name, task in tasks.items():
        if not isinstance(name, str) or not name:
            problems.append(f"task names must be nonempty strings: {name!r}")
        elif not callable(task):
            problems.append(f"task {name!r} is not callable")
        elif _is_async_task(task):
            problems.append(f"task {name!r} must be synchronous, not async")
    missing = sorted(map(repr, tasks.keys() - dag.keys()))
    extra = sorted(map(repr, dag.keys() - tasks.keys()))
    if missing:
        problems.append(f"tasks missing from dag: {', '.join(missing)}")
    if extra:
        problems.append(f"dag entries without a task: {', '.join(extra)}")
    unknown: set[str] = set()
    for deps in dag.values():
        unknown.update(deps - tasks.keys())
    if unknown:
        problems.append(f"unknown prerequisites: {', '.join(sorted(map(repr, unknown)))}")
    return problems


# --- Run events ---------------------------------------------------------------

EventKind = Literal["run_started", "task_started", "task_completed", "run_completed",
                    "attempt_started", "attempt_completed", "retry_scheduled"]
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
    attempt: int | None = None
    max_attempts: int | None = None
    delay_seconds: float | None = None
    failure_reason: Literal["exhausted", "non_retryable"] | None = None

    @classmethod
    def from_dict(cls, data: object) -> Event | None:
        """Parse a JSON event, or return None if it isn't one of this schema."""
        if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] not in (1, 2):
            return None
        kinds = ("run_started", "task_started", "task_completed", "run_completed")
        if data["schema"] == 2:
            kinds += ("attempt_started", "attempt_completed", "retry_scheduled")
        if data.get("event") not in kinds:
            return None
        fields = {key: data.get(key) for key in ("workflow", "run_id", "timestamp", "task", "status", "error")}
        if not all(isinstance(fields[key], str) and fields[key] for key in ("workflow", "run_id", "timestamp")):
            return None
        if any(fields[key] is not None and not isinstance(fields[key], str) for key in ("task", "status", "error")):
            return None
        kind = data["event"]
        extra = {key: data.get(key) for key in ("attempt", "max_attempts", "delay_seconds", "failure_reason")}
        if kind.startswith("attempt_") or kind == "retry_scheduled":
            if (type(extra["attempt"]) is not int or type(extra["max_attempts"]) is not int
                    or not 1 <= extra["attempt"] <= extra["max_attempts"]):
                return None
        elif extra["attempt"] is not None or extra["max_attempts"] is not None:
            return None
        if kind == "retry_scheduled":
            delay = extra["delay_seconds"]
            if not _valid_delay(delay) or extra["attempt"] >= extra["max_attempts"]:
                return None
        elif extra["delay_seconds"] is not None:
            return None
        if extra["failure_reason"] is not None:
            if (kind != "task_completed" or fields["status"] != "failed"
                    or extra["failure_reason"] not in ("exhausted", "non_retryable")
                    or data["schema"] != 2):
                return None
        if kind.startswith(("task_", "attempt_")) or kind == "retry_scheduled":
            if not fields["task"]:
                return None
        elif fields["task"] is not None:
            return None
        if kind.endswith("_completed"):
            if fields["status"] not in ("succeeded", "failed"):
                return None
        elif fields["status"] is not None:
            return None
        if fields["error"] is not None and fields["status"] != "failed":
            return None
        try:
            timestamp = datetime.fromisoformat(fields["timestamp"])
            if timestamp.utcoffset() is None:
                return None
            timestamp.astimezone(UTC)  # also reject offsets that overflow datetime's range
        except (ValueError, OverflowError):
            return None
        return cls(event=kind, schema=data["schema"], **fields, **extra)

    def to_dict(self) -> dict:
        data = {"schema": self.schema, "event": self.event, "workflow": self.workflow,
                "run_id": self.run_id, "timestamp": self.timestamp}
        for key in ("task", "status", "error", "attempt", "max_attempts", "delay_seconds", "failure_reason"):
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
class AttemptRun:
    """Recorded evidence of one task call; absent starts are not synthesized."""

    number: int
    status: RunStatus
    started: datetime | None = None
    finished: datetime | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {"number": self.number, "status": self.status, "started": _iso(self.started),
                "finished": _iso(self.finished), "error": self.error}


@dataclass(frozen=True)
class TaskRun:
    """One task within a recorded run. `incomplete`: the run ended mid-task."""

    name: str
    status: RunStatus
    started: datetime | None = None
    finished: datetime | None = None
    error: str | None = None
    attempts: tuple[AttemptRun, ...] = ()
    max_attempts: int | None = None
    retry_at: datetime | None = None
    failure_reason: str | None = None

    @property
    def duration(self) -> timedelta | None:
        return _seconds(self.started, self.finished)

    def to_dict(self) -> dict:
        duration = self.duration
        return {"attempts": [attempt.to_dict() for attempt in self.attempts],
                "max_attempts": self.max_attempts, "retry_at": _iso(self.retry_at),
                "failure_reason": self.failure_reason,
                "name": self.name, "status": self.status, "started": _iso(self.started),
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
    "timeout", "signal"). `truncated` means a bounded query omitted older
    records: tasks may be missing, and a missing start has no time or duration.
    """

    workflow: str
    run_id: str
    status: RunStatus
    started: datetime | None
    finished: datetime | None = None
    error: str | None = None
    tasks: tuple[TaskRun, ...] = ()
    result: str | None = None
    truncated: bool = False

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
                "error": self.error, "result": self.result, "tasks": [t.to_dict() for t in self.tasks],
                "truncated": self.truncated}


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
    attempt: int | None = None

    def to_dict(self) -> dict:
        return {"attempt": self.attempt, "timestamp": _iso(self.timestamp), "kind": self.kind, "message": self.message,
                "task": self.task, "priority": self.priority,
                "event": None if self.event is None else self.event.to_dict()}


@dataclass(frozen=True)
class RunLogs:
    run: Run
    lines: tuple[LogLine, ...]

    def to_dict(self) -> dict:
        return {"run": self.run.to_dict(), "lines": [line.to_dict() for line in self.lines]}


# --- Inventory --------------------------------------------------------------

@dataclass(frozen=True)
class InstalledWorkflow:
    """A workflow installed by floe, as recorded in its unit files.

    `problems` are found without running any workflow code: a missing unit of
    the pair, a missing script, or units pointing at floe in another place.
    """

    name: str
    script: Path | None
    service: Path | None
    timer: Path | None
    problems: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"name": self.name, "script": None if self.script is None else str(self.script),
                "service": None if self.service is None else str(self.service),
                "timer": None if self.timer is None else str(self.timer), "problems": list(self.problems)}


@dataclass(frozen=True)
class WorkflowSummary:
    """An installed workflow with its schedule, timer state, and last run.

    timer_state holds the queried properties, or None after a failed query.
    Unlike the convenience booleans, it distinguishes missing/unknown evidence
    from a known inactive or disabled state.
    """

    workflow: InstalledWorkflow
    schedule: tuple[str, ...] = ()
    enabled: bool = False
    active: bool = False
    next_run: datetime | None = None
    last_trigger: datetime | None = None
    last_run: Run | None = None
    problems: tuple[str, ...] = ()
    timer_state: UnitState | None = None

    @property
    def name(self) -> str:
        return self.workflow.name

    def to_dict(self) -> dict:
        return {**self.workflow.to_dict(), "schedule": list(self.schedule), "enabled": self.enabled,
                "active": self.active, "next_run": _iso(self.next_run), "last_trigger": _iso(self.last_trigger),
                "last_run": None if self.last_run is None else self.last_run.to_dict(),
                "problems": list(self.problems),
                "timer_state": None if self.timer_state is None else self.timer_state.to_dict()}
