"""Data types and errors shared by the library and every UI.

Everything a UI receives from floe is one of these types. They hold data only:
formatting for humans (text, HTML) belongs to the UI, and `to_dict()` gives a
JSON-ready form for machine interfaces.
"""

import difflib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
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
    """A workflow definition or setting is invalid. Lists every problem found."""

    def __init__(self, problems: Iterable[str]):
        self.problems = tuple(problems)
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
            raise ValidationError(problems)
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
