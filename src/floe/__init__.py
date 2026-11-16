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
from .loader import load
from .model import (
    EVENT_SCHEMA,
    Event,
    FloeError,
    InstallPlan,
    LoadError,
    NotOwnedError,
    SystemdError,
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
    "EVENT_SCHEMA", "Deployment", "Event", "FloeError", "InstallPlan", "LoadError",
    "NotOwnedError", "Systemd", "SystemdError", "UninstallError", "UnitChange", "UnitState",
    "UnitStore", "ValidationError", "Workflow", "apply", "install", "load", "plan_install",
    "run", "start", "status", "uninstall", "unit_names", "user_unit_dir",
]
