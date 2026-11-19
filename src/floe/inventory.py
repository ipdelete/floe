"""Discover installed workflows from the unit files floe wrote.

There is no registry: each unit's [X-Floe] section records the workflow name,
its definition file, and the floe source directory the service uses.
"""

import os
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .deploy import PACKAGE_ROOT, Deployment, plan_install, unit_names
from .history import list_runs
from .journal import Journal
from .loader import load
from .model import FloeError, InstalledWorkflow, NotFoundError, UnitState, ValidationError, WorkflowSummary
from .store import UnitStore
from .systemd import Systemd

__all__ = ["check_installed", "find_installed", "installed_workers", "installed_timeout", "list_installed", "overview"]

_CALENDAR = re.compile(r"\{ OnCalendar=(.*?) ; next_elapse=")
_TIMER_PROPS = ("UnitFileState", "ActiveState", "TimersCalendar", "NextElapseUSecRealtime", "LastTriggerUSec")


@dataclass
class _UnitPair:
    service: Path | None = None
    timer: Path | None = None
    metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def list_installed(store: UnitStore, *, package_root: Path = PACKAGE_ROOT) -> list[InstalledWorkflow]:
    """Every workflow with at least one floe-owned unit file, sorted by name.
    Reads only unit files; never runs workflow code or calls systemd."""
    found: dict[str, _UnitPair] = {}
    for unit in store.units():
        meta = store.metadata(unit)
        if meta.get("Owner") != "floe":
            continue
        stem, kind = unit.rsplit(".", 1)
        entry = found.setdefault(meta.get("Workflow") or stem, _UnitPair())
        if meta.get("Workflow") != stem:
            entry.problems.append(f"{unit} records workflow {meta.get('Workflow')!r}")
        if kind == "service":
            entry.service = store.path / unit
        else:
            entry.timer = store.path / unit
        entry.metadata[kind] = meta
    return [_installed(name, entry, package_root) for name, entry in sorted(found.items())]


def _installed(name: str, entry: _UnitPair, package_root: Path) -> InstalledWorkflow:
    problems = list(entry.problems)
    service, timer = entry.service, entry.timer
    if any(meta.get("Cleanup") == "pending" for meta in entry.metadata.values()):
        problems.append(f"timer-state cleanup pending; run floe uninstall --name {name}")
        return InstalledWorkflow(name, None, service, timer, tuple(problems))
    for kind, path in (("service", service), ("timer", timer)):
        if path is None:
            problems.append(f"{kind} unit is missing; reinstall")
    if service is not None:
        python = entry.metadata.get("service", {}).get("Python")
        if python is None:
            problems.append("installed without an interpreter environment; reinstall")
        elif not Path(python).is_absolute() or not Path(python).is_file() or not os.access(python, os.X_OK):
            problems.append(f"Python interpreter missing or not executable: {python}; reinstall")
        elif python != os.path.abspath(sys.executable):
            problems.append(f"uses Python from {python}; reinstall to use {os.path.abspath(sys.executable)}")
    scripts = {meta.get("Script") for meta in entry.metadata.values()}
    roots = {meta.get("Floe") for meta in entry.metadata.values()}
    script = None
    if None in scripts or None in roots:
        problems.append("installed by an older floe version; reinstall from its definition file")
    elif len(scripts) > 1 or len(roots) > 1:
        problems.append("service and timer were installed differently; reinstall")
    else:
        script = Path(scripts.pop())
        if not script.is_file():
            problems.append(f"script not found: {script}")
        root = Path(roots.pop())
        if not (root / "floe_cli").is_dir():
            problems.append("floe has moved since installation; reinstall")
        elif root.resolve() != package_root.resolve():
            problems.append(f"uses floe from {root}; reinstall to use {package_root}")
    return InstalledWorkflow(name, script, service, timer, tuple(problems))


def find_installed(store: UnitStore, name: str) -> InstalledWorkflow:
    """The installed workflow with this name, or NotFoundError."""
    unit_names(name)  # validates the name
    for installed in list_installed(store):
        if installed.name == name:
            return installed
    raise NotFoundError(f"workflow {name!r} is not installed")


def installed_workers(store: UnitStore, name: str) -> int:
    """Recorded worker limit of an owned service; 1 for a new or legacy install."""
    service, _ = unit_names(name)
    if not store.is_owned(service):
        return 1
    value = store.metadata(service).get('Workers', '1')
    try:
        workers = int(value)
    except ValueError:
        workers = 0
    if workers < 1:
        raise ValidationError(['invalid installed Workers metadata; reinstall with --workers'])
    return workers


def installed_timeout(store: UnitStore, name: str) -> str:
    """Recorded start timeout, or the default for a new or legacy install."""
    service, _ = unit_names(name)
    return store.metadata(service).get('Timeout', '30min') if store.is_owned(service) else '30min'


def check_installed(installed: InstalledWorkflow, store: UnitStore) -> list[str]:
    """Whether the installed units still match the definition file.

    This loads the definition, so it runs the workflow file's top-level code.
    """
    if installed.script is None or not installed.script.is_file():
        return []  # already reported by list_installed
    try:
        workflow = load(installed.script)
        plan = plan_install(Deployment.for_workflow(workflow, workers=installed_workers(store, installed.name),
                                                   timeout=installed_timeout(store, installed.name)), store)
    except FloeError as exc:
        return [f"definition does not load: {exc}"]
    if workflow.name != installed.name:
        return [f"definition now names workflow {workflow.name!r}; uninstall {installed.name!r} and reinstall"]
    stale = [change.name for change in plan.changes if change.action != "unchanged"]
    return [f"units out of date ({', '.join(stale)}); reinstall"] if stale else []


def overview(store: UnitStore, sd: Systemd, journal: Journal, *, verify: bool = False,
             since: str | None = None, max_records: int | None = None) -> list[WorkflowSummary]:
    """Installed workflows with timer state, schedule, next/last trigger, and last run.

    With verify, also `check_installed` each definition (runs workflow code). systemd or
    journal errors for one workflow become problems instead of failing all.
    `since` and `max_records` bound history reads, as in list_runs.
    """
    summaries: list[WorkflowSummary] = []
    for installed in list_installed(store):
        problems = list(installed.problems)
        if verify:
            problems += check_installed(installed, store)
        _, timer = unit_names(installed.name)
        summary = WorkflowSummary(installed)
        try:
            props = sd.show_all(timer, *_TIMER_PROPS, unix_timestamps=True)
            values = dict(props)
            if values.get("ActiveState") != "active" and installed.timer is not None:
                problems.append(f"timer is {values.get('ActiveState') or 'not loaded'}")
            summary = replace(
                summary,
                schedule=tuple(m.group(1) for key, value in props if key == "TimersCalendar"
                               if (m := _CALENDAR.search(value))),
                enabled=values.get("UnitFileState") == "enabled",
                active=values.get("ActiveState") == "active",
                timer_state=UnitState(timer, values),
                next_run=_timestamp(values.get("NextElapseUSecRealtime", "")),
                last_trigger=_timestamp(values.get("LastTriggerUSec", "")),
            )
        except (FloeError, OSError) as exc:
            problems.append(str(exc))
        try:
            runs = list_runs(installed.name, journal, sd, limit=1, since=since, max_records=max_records)
            summary = replace(summary, last_run=runs[0] if runs else None)
        except (FloeError, OSError) as exc:
            problems.append(str(exc))
        summaries.append(replace(summary, problems=tuple(problems)))
    return summaries


def _timestamp(value: str) -> datetime | None:
    """systemd's --timestamp=unix form, "@<seconds>"; empty when never/not scheduled."""
    if not value.startswith("@"):
        return None
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=float(value[1:]))
    except ValueError:
        return None
