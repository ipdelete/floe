"""Minimal local Python workflows with systemd scheduling and journald history.

This package is the library every UI (the `floe` command and local web UI)
builds on. It never prints, parses arguments, or exits: functions return the
data types in `floe.model` or raise `FloeError` subclasses.
"""

from .deploy import (
    Deployment,
    apply,
    cancel,
    install,
    plan_install,
    start,
    status,
    uninstall,
    unit_names,
    user_unit_dir,
)
from .history import get_run, list_runs, run_logs
from .inventory import check_installed, find_installed, installed_timeout, installed_workers, list_installed, overview
from .journal import Journal
from .loader import load
from .managed import run_managed
from .model import (
    EVENT_SCHEMA,
    AttemptRun,
    RetryPolicy,
    Event,
    FloeError,
    InstalledWorkflow,
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
    WorkflowSummary,
    parallel,
)
from .runner import run
from .store import UnitStore
from .systemd import Systemd

__all__ = [
    "AttemptRun", "RetryPolicy", "EVENT_SCHEMA", "Deployment", "Event", "FloeError", "InstallPlan", "InstalledWorkflow",
    "Journal", "LoadError", "LogLine", "NotFoundError", "NotOwnedError", "Run", "RunLogs",
    "Systemd", "SystemdError", "TaskRun", "UninstallError", "UnitChange", "UnitState",
    "UnitStore", "ValidationError", "Workflow", "WorkflowSummary", "apply", "cancel", "check_installed",
    "find_installed", "get_run", "install", "installed_timeout", "installed_workers", "list_installed", "list_runs", "load", "overview",
    "parallel", "plan_install", "run", "run_logs", "run_managed", "start", "status", "uninstall", "unit_names",
    "user_unit_dir",
]
