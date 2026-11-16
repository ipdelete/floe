"""Deploy a stdlib-only workflow script as a systemd user service and timer.

Rendering and file storage stay separate from the thin systemctl wrapper.
Only this module combines them; the workflow execution path never imports it.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .render import check_value, render_service, render_timer
from .store import UnitStore
from .systemd import Systemd, SystemdError


@dataclass(frozen=True)
class Deployment:
    workflow: str
    script: Path
    schedule: tuple[str, ...]
    python: str = "/usr/bin/python3"
    timeout: str = "30min"
    persistent: bool = True

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,246}", self.workflow):
            raise ValueError(
                "workflow must be 1–247 characters: letters, digits, _, ., -; "
                "start with a letter or digit"
            )
        if isinstance(self.schedule, str):
            raise ValueError("schedule must be a sequence of OnCalendar expressions, not a string")
        schedule = tuple(self.schedule)
        if not schedule:
            raise ValueError("schedule must contain at least one OnCalendar expression")
        for expression in schedule:
            check_value(expression, "schedule")
        object.__setattr__(self, "schedule", schedule)
        object.__setattr__(self, "script", Path(self.script))

    @property
    def service_name(self) -> str:
        return f"{self.workflow}.service"

    @property
    def timer_name(self) -> str:
        return f"{self.workflow}.timer"

    def render_units(self) -> dict[str, str]:
        script = self.script.resolve()
        return {
            self.service_name: render_service(self.workflow, script, self.python, self.timeout),
            self.timer_name: render_timer(self.workflow, self.schedule, self.persistent),
        }


def user_unit_dir() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME")
    # XDG base directories must be absolute; ignore empty or relative values.
    base = Path(config) if config and Path(config).is_absolute() else Path.home() / ".config"
    return base / "systemd" / "user"


def install(dep: Deployment, store: UnitStore, sd: Systemd, *, adopt: bool = False) -> list[str]:
    """Render and preflight the pair before writing. Each write is atomic."""
    wanted = dep.render_units()
    for name in wanted:
        store.preflight(name, adopt=adopt)
    changed = [name for name, text in wanted.items() if store.write(name, text, adopt=adopt)]
    if changed:
        sd.daemon_reload()
    sd.enable(dep.timer_name, now=True)
    return changed


def uninstall(dep: Deployment, store: UnitStore, sd: Systemd) -> list[str]:
    owned = [name for name in (dep.timer_name, dep.service_name) if store.is_owned(name)]
    if dep.timer_name in owned:
        sd.disable(dep.timer_name, now=True)
    if dep.service_name in owned:
        sd.stop(dep.service_name)
    removed = [name for name in owned if store.remove(name)]
    if removed:
        sd.daemon_reload()
    return removed


STATUS_PROPS = {
    "service": ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus",
                "ExecMainStartTimestamp", "ExecMainExitTimestamp", "InvocationID", "NeedDaemonReload"),
    "timer": ("LoadState", "ActiveState", "UnitFileState", "LastTriggerUSec",
              "NextElapseUSecRealtime", "NeedDaemonReload"),
}


def status(dep: Deployment, sd: Systemd) -> dict[str, dict[str, str]]:
    return {
        dep.service_name: sd.show(dep.service_name, *STATUS_PROPS["service"]),
        dep.timer_name: sd.show(dep.timer_name, *STATUS_PROPS["timer"]),
    }


def cli(
    dep: Deployment,
    argv: list[str],
    *,
    store: UnitStore | None = None,
    sd: Systemd | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=dep.script.name, description=f"Manage workflow {dep.workflow!r}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="write unit files, reload systemd, enable the timer")
    p.add_argument("--adopt", action="store_true", help="take over existing unit files not written by floe")
    p.add_argument("--dry-run", action="store_true", help="print the unit files and do nothing")
    sub.add_parser("uninstall", help="disable and stop, then delete the unit files")
    sub.add_parser("start", help="run the workflow now under systemd and wait for it")
    sub.add_parser("status", help="show systemd state for the service and timer")
    args = parser.parse_args(argv)

    try:
        store = UnitStore(user_unit_dir()) if store is None else store
        sd = Systemd() if sd is None else sd
        if args.cmd == "install" and args.dry_run:
            for name, text in dep.render_units().items():
                print(f"# {store.path / name}\n{text}")
        elif args.cmd == "install":
            changed = install(dep, store, sd, adopt=args.adopt)
            print(f"wrote: {', '.join(changed) or 'nothing (up to date)'}; enabled {dep.timer_name}")
        elif args.cmd == "uninstall":
            removed = uninstall(dep, store, sd)
            print(f"removed: {', '.join(removed) or 'nothing'}")
        elif args.cmd == "start":
            sd.start(dep.service_name)
            print(f"{dep.service_name} finished successfully")
        elif args.cmd == "status":
            for unit, props in status(dep, sd).items():
                print(unit)
                for key, value in props.items():
                    print(f"  {key}={value}")
    except (OSError, ValueError, SystemdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
