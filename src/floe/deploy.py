"""Deploy a workflow as a systemd user service and timer.

Rendering (render), file ownership (store) and systemctl (systemd) stay
separate; only this module combines them. Functions return data or raise
FloeError subclasses and never talk to the user, so any UI can drive them.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from .model import (
    InstallPlan,
    NotOwnedError,
    SystemdError,
    UninstallError,
    UnitChange,
    UnitState,
    ValidationError,
    Workflow,
    check_name,
    schedule_problems,
)
from .render import render_service, render_timer
from .store import UnitStore
from .systemd import Systemd

__all__ = [
    "Deployment", "apply", "install", "plan_install", "start", "status", "uninstall",
    "unit_names", "user_unit_dir",
]

# The directory containing the floe and floe_cli packages, for the service's
# PYTHONPATH. With an editable install this is the repository's src directory.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

STATUS_PROPS = {
    "service": ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus",
                "ExecMainStartTimestamp", "ExecMainExitTimestamp", "InvocationID", "NeedDaemonReload"),
    "timer": ("LoadState", "ActiveState", "UnitFileState", "LastTriggerUSec",
              "NextElapseUSecRealtime", "NeedDaemonReload"),
}


def unit_names(workflow: str) -> tuple[str, str]:
    """(service, timer) unit names for a workflow name."""
    check_name(workflow)
    return f"{workflow}.service", f"{workflow}.timer"


def user_unit_dir() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME")
    # XDG base directories must be absolute; ignore empty or relative values.
    base = Path(config) if config and Path(config).is_absolute() else Path.home() / ".config"
    return base / "systemd" / "user"


@dataclass(frozen=True)
class Deployment:
    """How a workflow is deployed: the settings that shape its unit files."""

    workflow: str
    script: Path
    schedule: tuple[str, ...]
    python: str = "/usr/bin/python3"
    timeout: str = "30min"
    persistent: bool = True
    pythonpath: Path = PACKAGE_ROOT

    def __post_init__(self) -> None:
        check_name(self.workflow)
        if problems := schedule_problems(self.schedule):
            raise ValidationError(problems)
        schedule = tuple(self.schedule)
        if not schedule:
            raise ValidationError(["schedule must contain at least one OnCalendar expression"])
        object.__setattr__(self, "schedule", schedule)
        object.__setattr__(self, "script", Path(self.script))
        object.__setattr__(self, "pythonpath", Path(self.pythonpath))

    @classmethod
    def for_workflow(cls, workflow: Workflow, **settings) -> Deployment:
        if workflow.source is None:
            raise ValidationError([f"workflow {workflow.name!r} has no source file to deploy"])
        return cls(workflow.name, workflow.source, workflow.schedule, **settings)

    @property
    def service_name(self) -> str:
        return f"{self.workflow}.service"

    @property
    def timer_name(self) -> str:
        return f"{self.workflow}.timer"

    def render_units(self) -> dict[str, str]:
        script = self.script.resolve()
        try:
            return {
                self.service_name: render_service(
                    self.workflow, script, self.python, self.timeout, self.pythonpath),
                self.timer_name: render_timer(self.workflow, self.schedule, self.persistent),
            }
        except ValueError as exc:
            raise ValidationError([str(exc)]) from exc


def plan_install(dep: Deployment, store: UnitStore, *, adopt: bool = False) -> InstallPlan:
    """Describe what installing would change. Reads files; writes nothing."""
    changes = []
    for name, text in dep.render_units().items():
        path = store.path / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            changes.append(UnitChange(name, path, "conflict", text,
                                      reason="not a regular file (masked or linked?)"))
            continue
        if not path.exists():
            changes.append(UnitChange(name, path, "create", text))
            continue
        current = path.read_text()
        if not store.is_owned(name):
            if adopt:
                changes.append(UnitChange(name, path, "adopt", text, current))
            else:
                changes.append(UnitChange(name, path, "conflict", text, current,
                                          reason="not managed by floe (adopt to take over)"))
        elif current == text:
            changes.append(UnitChange(name, path, "unchanged", text, current))
        else:
            changes.append(UnitChange(name, path, "update", text, current))
    return InstallPlan(dep.workflow, dep.timer_name, tuple(changes))


def apply(plan: InstallPlan, store: UnitStore, sd: Systemd) -> list[str]:
    """Write a plan's changes, reload systemd if needed, and enable the timer.

    Refuses plans with conflicts. Each write rechecks ownership and is atomic;
    the pair is not a transaction, so a failure may require applying again.
    """
    if plan.conflicts:
        raise NotOwnedError("; ".join(f"{c.path}: {c.reason}" for c in plan.conflicts))
    changed = [c.name for c in plan.writes if store.write(c.name, c.text, adopt=c.action == "adopt")]
    if changed:
        sd.daemon_reload()
    sd.enable(plan.timer, now=True)
    return changed


def install(dep: Deployment, store: UnitStore, sd: Systemd, *, adopt: bool = False) -> list[str]:
    """plan_install then apply; returns the unit files written."""
    return apply(plan_install(dep, store, adopt=adopt), store, sd)


_RUNNING = ("active", "activating", "deactivating", "reloading", "refreshing")


def uninstall(workflow: str, store: UnitStore, sd: Systemd) -> list[str]:
    """Best effort: attempt every step, then verify, then report all failures.

    A unit is ours if its file is owned, or if the file is gone but systemd
    still has it loaded from our path (an earlier uninstall was interrupted).
    Foreign, masked, or linked files and same-named units elsewhere are left alone.
    Rerunning after a failure continues where the previous attempt stopped.
    """
    service, timer = unit_names(workflow)
    names = (timer, service)
    errors: list[str] = []

    def attempt(fn, *args, **kwargs) -> bool:
        try:
            fn(*args, **kwargs)
            return True
        except (OSError, SystemdError) as exc:
            errors.append(str(exc))
            return False

    def loaded_from_store(name: str) -> bool:
        try:
            props = sd.show(name, "LoadState", "FragmentPath")
        except SystemdError as exc:
            errors.append(str(exc))
            return False
        return props.get("LoadState") != "not-found" and store.owns_path(name, props.get("FragmentPath", ""))

    owned = {name for name in names if store.is_owned(name)}
    present = {name for name in names if (store.path / name).exists() or (store.path / name).is_symlink()}
    stale = {name for name in names if name not in present and loaded_from_store(name)}
    ours = [name for name in names if name in owned or name in stale]

    # Stop new activations first, then the current run.
    if timer in owned:
        if not attempt(sd.disable, timer, now=True):
            attempt(sd.stop, timer, missing_ok=True)
    elif timer in stale:
        attempt(sd.stop, timer, missing_ok=True)
    if service in ours:
        attempt(sd.stop, service, missing_ok=True)

    # Remove files even if stopping failed: the timer can no longer start new
    # runs, and systemd keeps managing a running process until it exits.
    removed = []
    for name in names:
        if name in owned:
            if attempt(store.remove, name):
                removed.append(name)
        elif name not in present:
            attempt(store.remove_links, name)  # dangling links from an interrupted run

    if not ours:
        return removed
    attempt(sd.daemon_reload)

    for name in ours:
        try:
            props = sd.show(name, "LoadState", "ActiveState", "FragmentPath")
        except SystemdError as exc:
            errors.append(str(exc))
            continue
        fragment = props.get("FragmentPath", "")
        if props.get("LoadState") != "not-found" and store.owns_path(name, fragment):
            errors.append(f"{name} is still loaded from {fragment}")
        elif props.get("ActiveState") in _RUNNING and (not fragment or store.owns_path(name, fragment)):
            errors.append(f"{name} is still {props['ActiveState']}")
    if errors:
        raise UninstallError(removed, errors)
    return removed


def start(workflow: str, sd: Systemd, *, wait: bool = True) -> None:
    """Run the service now. With wait, returns after the oneshot run finishes
    and raises SystemdError if it failed."""
    service, _ = unit_names(workflow)
    sd.start(service, wait=wait)


def status(workflow: str, sd: Systemd) -> dict[str, UnitState]:
    """Current systemd state of the service and timer, keyed by unit name."""
    service, timer = unit_names(workflow)
    return {
        service: UnitState(service, sd.show(service, *STATUS_PROPS["service"])),
        timer: UnitState(timer, sd.show(timer, *STATUS_PROPS["timer"])),
    }
