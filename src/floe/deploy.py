"""Deploy a workflow as a systemd user service and timer.

Rendering (render), file ownership (store) and systemctl (systemd) stay
separate; only this module combines them. Functions return data or raise
FloeError subclasses and never talk to the user, so any UI can drive them.
"""

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .managed import check_managed, request_cancel
from .model import (
    InstallPlan,
    NotFoundError,
    NotOwnedError,
    SystemdError,
    UninstallError,
    UnitChange,
    UnitState,
    ValidationError,
    Workflow,
    check_name,
    normalize_schedule,
)
from .render import render_service, render_timer
from .store import UnitStore
from .systemd import Systemd, validate_calendars, validate_timeout

__all__ = [
    "Deployment", "apply", "cancel", "install", "plan_install", "start", "status", "uninstall",
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
    python: str = field(default_factory=lambda: os.path.abspath(sys.executable))
    timeout: str = "30min"
    persistent: bool = True
    pythonpath: Path = PACKAGE_ROOT
    workers: int = 1

    def __post_init__(self) -> None:
        check_name(self.workflow)
        if type(self.workers) is not int or self.workers < 1:
            raise ValidationError(['worker limit must be a positive integer'])
        schedule, problems = normalize_schedule(self.schedule)
        if problems:
            raise ValidationError(problems)
        if not schedule:
            raise ValidationError(["schedule must contain at least one OnCalendar expression"])
        object.__setattr__(self, "schedule", schedule)
        object.__setattr__(self, "script", Path(self.script))
        object.__setattr__(self, "pythonpath", Path(self.pythonpath))

    @classmethod
    def for_workflow(cls, workflow: Workflow, **settings) -> Deployment:
        if workflow.source is None:
            raise ValidationError([f"workflow {workflow.name!r} has no source file to deploy"])
        deployment = cls(workflow.name, workflow.source, workflow.schedule, **settings)
        if deployment.workers > 1:
            check_managed(workflow, deployment.workers)
        return deployment

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
                    self.workflow, script, self.python, self.timeout, self.pythonpath, self.workers),
                self.timer_name: render_timer(
                    self.workflow, self.schedule, self.persistent, script, self.pythonpath),
            }
        except ValueError as exc:
            raise ValidationError([str(exc)]) from exc


def plan_install(dep: Deployment, store: UnitStore, *, adopt: bool = False) -> InstallPlan:
    """Validate calendars/timeout and plan changes, without writes or systemctl."""
    units = dep.render_units()
    validate_timeout(dep.timeout)
    validate_calendars(dep.schedule)
    _validate_python(dep)
    changes = []
    for name, text in units.items():
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


def _validate_python(dep: Deployment) -> None:
    """Check imports without executing workflow code or contacting the manager."""
    code = ('import sys, importlib.util; '
            'sys.version_info >= (3, 14) or sys.exit("Python 3.14+ required"); '
            'import tenacity; import floe; '
            'importlib.util.find_spec("floe_cli") is not None or sys.exit("floe_cli is missing")')
    try:
        result = subprocess.run([dep.python, "-B", "-c", code], capture_output=True, text=True,
                                env={"PATH": os.defpath, "PYTHONPATH": str(dep.pythonpath)}, cwd="/", timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError([f"cannot validate Python environment {dep.python}: {exc}"]) from exc
    if result.returncode:
        raise ValidationError([f"Python environment {dep.python} must provide Floe and Tenacity: "
                               f"{result.stderr.strip()}"])


def apply(plan: InstallPlan, store: UnitStore, sd: Systemd) -> list[str]:
    """Write a plan's changes, reload systemd if needed, and enable the timer.

    Refuses plans with conflicts. Each write rechecks ownership and is atomic;
    the pair is not a transaction, so a failure may require applying again.
    """
    if plan.conflicts:
        raise NotOwnedError("; ".join(f"{c.path}: {c.reason}" for c in plan.conflicts))
    changed = []
    for change in plan.writes:
        if store.write(change.name, change.text, adopt=change.action == "adopt"):
            changed.append(change.name)
    if changed:
        sd.daemon_reload()
    sd.enable(plan.timer, now=True)
    return changed


def install(dep: Deployment, store: UnitStore, sd: Systemd, *, adopt: bool = False) -> list[str]:
    """plan_install then apply; returns the unit files written."""
    return apply(plan_install(dep, store, adopt=adopt), store, sd)


_RUNNING = ("active", "activating", "deactivating", "reloading", "refreshing")


@dataclass(frozen=True)
class _UninstallUnits:
    owned_files: set[str]
    present_files: set[str]
    safe_to_stop: set[str]
    unknown: set[str]


def _classify_uninstall(names: tuple[str, str], store: UnitStore, sd: Systemd,
                        errors: list[str]) -> _UninstallUnits:
    """Keep file ownership, loaded-unit ownership, and failed queries distinct."""
    owned_files = {name for name in names if store.is_owned(name)}
    present_files = {name for name in names
                     if (store.path / name).exists() or (store.path / name).is_symlink()}
    safe_to_stop: set[str] = set()
    unknown: set[str] = set()
    for name in names:
        if name in present_files and name not in owned_files:
            continue
        try:
            props = sd.show(name, "LoadState", "FragmentPath")
        except (OSError, SystemdError) as exc:
            errors.append(str(exc))
            unknown.add(name)
            continue  # unknown ownership: do not stop a unit
        fragment = props.get("FragmentPath", "")
        if store.owns_path(name, fragment) or (
            not fragment and props.get("LoadState") == "not-found" and name in owned_files
        ):
            safe_to_stop.add(name)
    return _UninstallUnits(owned_files, present_files, safe_to_stop, unknown)


def _owned_unit_state(name: str, store: UnitStore, sd: Systemd,
                      errors: list[str]) -> dict[str, str] | None:
    try:
        props = sd.show(name, "LoadState", "ActiveState", "FragmentPath")
    except (OSError, SystemdError) as exc:
        errors.append(str(exc))
        return None
    fragment = props.get("FragmentPath", "")
    # Only judge a unit that is (or was) ours, not a same-named one elsewhere.
    if fragment and not store.owns_path(name, fragment):
        return None
    return props


def _verify_unloaded(names: list[str], store: UnitStore, sd: Systemd, errors: list[str]) -> None:
    # A failed unit lingers in memory after removal until its state is reset.
    for name in names:
        props = _owned_unit_state(name, store, sd, errors)
        if props is not None and props.get("ActiveState") == "failed":
            try:
                sd.reset_failed(name)
            except (OSError, SystemdError) as exc:
                errors.append(str(exc))

    for name in names:
        props = _owned_unit_state(name, store, sd, errors)
        if props is None:
            continue
        if props.get("LoadState") != "not-found":
            errors.append(f"{name} is still loaded from {props.get('FragmentPath')}")
        elif props.get("ActiveState") in (*_RUNNING, "failed"):
            errors.append(f"{name} is still {props['ActiveState']}")


def uninstall(workflow: str, store: UnitStore, sd: Systemd) -> list[str]:
    """Best effort: attempt every step, then verify, then report all failures.

    Owned files can be removed independently of loaded-unit ownership. Lifecycle
    operations require a fragment from our store (or an owned file whose unit is
    not loaded). Missing files may still have stale loaded units from an earlier
    interrupted uninstall. Foreign, masked, or linked files and same-named units
    loaded elsewhere are left alone. Rerunning continues an interrupted attempt.
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

    units = _classify_uninstall(names, store, sd, errors)
    units_to_verify = [name for name in names if name in units.owned_files or name in units.safe_to_stop]
    retain_timer = timer in units.owned_files and timer in units.unknown

    # Stop new activations first, then the current run. An owned file alone
    # must not authorize stopping a same-named fragment loaded from elsewhere.
    if timer in units.safe_to_stop:
        if timer in units.owned_files:
            if not attempt(sd.disable, timer, now=True):
                attempt(sd.stop, timer, missing_ok=True)
        else:
            attempt(sd.stop, timer, missing_ok=True)
    if service in units.safe_to_stop:
        attempt(sd.stop, service, missing_ok=True)
    if timer in units.safe_to_stop:
        # Keep proof of ownership until the last-trigger stamp is gone.
        retain_timer = not attempt(sd.clean_state, timer)
        if retain_timer and timer not in units.owned_files:
            # Recover an interrupted uninstall whose files were already gone.
            # A past-only calendar has no future trigger, even if a failed
            # stop left the old timer active when daemon-reload reads this.
            marker = (f"[Timer]\nOnCalendar=1970-01-01 00:00:00 UTC\nPersistent=yes\nUnit={service}\n\n"
                      "[Install]\nWantedBy=timers.target\n\n"
                      f"[X-Floe]\nOwner=floe\nWorkflow={workflow}\nCleanup=pending\n")
            attempt(store.write, timer, marker)

    # Continue removing files and enable links after failures, except for a
    # timer that must remain discoverable for retrying its state cleanup.
    removed = []
    for name in names:
        if name == timer and retain_timer:
            attempt(store.remove_links, name)
            errors.append(f"{timer}: timer-state cleanup is pending; rerun uninstall")
        elif name in units.owned_files:
            if attempt(store.remove, name):
                removed.append(name)
        elif name not in units.present_files:
            attempt(store.remove_links, name)  # dangling links from an interrupted run

    if units_to_verify:
        attempt(sd.daemon_reload)
        _verify_unloaded([name for name in units_to_verify if not (name == timer and retain_timer)],
                         store, sd, errors)
    if errors:
        raise UninstallError(removed, errors)
    return removed


def cancel(workflow: str, store: UnitStore, sd: Systemd, *, task: str | None = None) -> str:
    """Ask the current owned parallel invocation to cancel; return its full ID."""
    if task is not None and (not isinstance(task, str) or not task):
        raise ValidationError(['task must be a nonempty string'])
    service, _ = unit_names(workflow)
    props = sd.show(service, 'FragmentPath', 'InvocationID', 'MainPID', 'ActiveState')
    if not store.owns_path(service, props.get('FragmentPath', '')) or not store.is_owned(service):
        raise NotOwnedError(f'{service} is not loaded from a Floe-owned file')
    if props.get('ActiveState') not in ('activating', 'active') or not props.get('MainPID', '').isdigit():
        raise NotFoundError(f'no live supervisor for {workflow!r}')
    pid = int(props['MainPID'])
    if not pid:
        raise NotFoundError(f'no live supervisor for {workflow!r}')
    run_id = props.get('InvocationID', '')
    request_cancel(run_id, pid, task)
    return run_id


def start(workflow: str, sd: Systemd, *, wait: bool = True) -> None:
    """Run the service now. With wait, returns after the oneshot run finishes
    and raises SystemdError if it failed."""
    service, _ = unit_names(workflow)
    sd.start(service, wait=wait)


def status(workflow: str, sd: Systemd) -> dict[str, UnitState]:
    """Current systemd state of the service and timer, keyed by unit name.

    Raises NotFoundError when systemd knows neither unit (both not-found): no
    unit file and nothing loaded. A failed query raises SystemdError instead.
    """
    service, timer = unit_names(workflow)
    states = {
        service: UnitState(service, sd.show(service, *STATUS_PROPS["service"])),
        timer: UnitState(timer, sd.show(timer, *STATUS_PROPS["timer"])),
    }
    if all(state["LoadState"] == "not-found" for state in states.values()):
        raise NotFoundError(f"workflow {workflow!r} is not installed: systemd has no {service} or {timer}")
    return states
