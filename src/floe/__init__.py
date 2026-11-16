"""Minimal local Python workflows with systemd scheduling and journald history.

This package is the library every UI (the `floe` command, a future web UI)
builds on. It never prints, parses arguments, or exits: functions return the
data types in `floe.model` or raise `FloeError` subclasses.
"""

from .deploy import (
    Deployment,
    apply,
    install,
    plan_install,
    start,
    status,
    uninstall,
    unit_names,
    user_unit_dir,
)
from .history import get_run, list_runs, run_logs
from .journal import Journal
from .loader import load
from .model import (
    EVENT_SCHEMA,
    Event,
    FloeError,
    InstallPlan,
    LoadError,
    LogLine,
    NotFoundError,
    NotOwnedError,
    Run,
    RunLogs,
    SystemdError,
    TaskRun,
    UninstallError,
    UnitChange,
    UnitState,
    ValidationError,
    Workflow,
)
from .runner import run
from .store import UnitStore
from .systemd import Systemd

__all__ = [
    "EVENT_SCHEMA", "Deployment", "Event", "FloeError", "InstallPlan", "Journal", "LoadError",
    "LogLine", "NotFoundError", "NotOwnedError", "Run", "RunLogs", "Systemd", "SystemdError",
    "TaskRun", "UninstallError", "UnitChange", "UnitState", "UnitStore", "ValidationError",
    "Workflow", "apply", "get_run", "install", "list_runs", "load", "plan_install", "run",
    "run_logs", "start", "status", "uninstall", "unit_names", "user_unit_dir",
]
