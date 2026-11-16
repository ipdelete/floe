"""Deploy a workflow script as a systemd user service + timer.

    Deployment(...).units()       -> the unit files as models (pure, no I/O)
    install(dep, units, sd)       -> write files, daemon-reload, enable timer
    uninstall(dep, units, sd)     -> disable/stop, delete files, daemon-reload
    status(dep, sd)               -> systemd's view of both units
    cli(dep, argv)                -> argparse front end used by workflow scripts

Only this module combines the file layer (floe.units) with the service
manager (floe.systemd). The workflow runner itself never imports it, so the
service can run under a plain /usr/bin/python3 without pydantic.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from .systemd import Systemd, SystemdError
from .units import (
    FloeSection,
    InstallSection,
    NotOwnedError,
    ServiceFile,
    ServiceSection,
    TimerFile,
    TimerSection,
    UnitDir,
    UnitFile,
    UnitSection,
    exec_line,
)

USER_UNIT_DIR = Path("~/.config/systemd/user").expanduser()


@dataclass(frozen=True)
class Deployment:
    workflow: str
    script: Path
    schedule: list[str]
    python: str = "/usr/bin/python3"  # the runner is stdlib-only
    timeout: str = "30min"  # oneshot services have no start timeout by default
    persistent: bool = True

    @property
    def service_name(self) -> str:
        return f"{self.workflow}.service"

    @property
    def timer_name(self) -> str:
        return f"{self.workflow}.timer"

    def units(self) -> dict[str, UnitFile]:
        script = Path(self.script).resolve()
        marker = FloeSection(owner="floe", workflow=self.workflow)
        service = ServiceFile(
            unit=UnitSection(description=f"Floe workflow {self.workflow}"),
            service=ServiceSection(
                type="oneshot",
                exec_start=[exec_line([self.python, str(script), "run"])],
                working_directory=str(script.parent),
                timeout_start_sec=self.timeout,
                kill_mode="control-group",
                standard_output="journal",
                standard_error="journal",
                syslog_identifier=self.workflow,
            ),
            x_floe=marker,
        )
        timer = TimerFile(
            unit=UnitSection(description=f"Schedule floe workflow {self.workflow}"),
            timer=TimerSection(
                on_calendar=list(self.schedule), persistent=self.persistent, unit=self.service_name
            ),
            install=InstallSection(wanted_by=["timers.target"]),
            x_floe=marker,
        )
        return {self.service_name: service, self.timer_name: timer}


def install(dep: Deployment, units: UnitDir, sd: Systemd, *, adopt: bool = False) -> list[str]:
    """Idempotent. Returns the names of files that changed."""
    # Validate everything before writing anything.
    wanted = dep.units()
    if not adopt:
        for name in wanted:
            if (units.path / name).exists() and name not in units:
                raise NotOwnedError(f"{units.path / name} exists and is not managed by floe (use --adopt)")
    changed = [name for name, unit in wanted.items() if units.put(name, unit, force=adopt)]
    if changed:
        sd.daemon_reload()
    sd.enable(dep.timer_name, now=True)  # no-op if already enabled and active
    return changed


def uninstall(dep: Deployment, units: UnitDir, sd: Systemd) -> list[str]:
    removed = []
    if dep.timer_name in units:
        sd.disable(dep.timer_name, now=True)
    if dep.service_name in units:
        sd.stop(dep.service_name)
    for name in (dep.timer_name, dep.service_name):
        if name in units:
            del units[name]
            removed.append(name)
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


def cli(dep: Deployment, argv: list[str], *, units: UnitDir | None = None, sd: Systemd | None = None) -> int:
    parser = argparse.ArgumentParser(prog=Path(dep.script).name, description=f"Manage workflow {dep.workflow!r}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="write unit files, reload systemd, enable the timer")
    p.add_argument("--adopt", action="store_true", help="take over existing unit files not written by floe")
    p.add_argument("--dry-run", action="store_true", help="print the unit files and do nothing")
    sub.add_parser("uninstall", help="disable and stop, then delete the unit files")
    sub.add_parser("start", help="run the workflow now under systemd and wait for it")
    sub.add_parser("status", help="show systemd state for the service and timer")
    args = parser.parse_args(argv)

    # `is None`, not `or`: an empty UnitDir is falsy (it's a Mapping with len 0).
    units = UnitDir(USER_UNIT_DIR) if units is None else units
    sd = Systemd() if sd is None else sd
    try:
        if args.cmd == "install" and args.dry_run:
            for name, unit in dep.units().items():
                print(f"# {units.path / name}\n{units.render(unit)}")
        elif args.cmd == "install":
            changed = install(dep, units, sd, adopt=args.adopt)
            print(f"wrote: {', '.join(changed) or 'nothing (up to date)'}; enabled {dep.timer_name}")
        elif args.cmd == "uninstall":
            removed = uninstall(dep, units, sd)
            print(f"removed: {', '.join(removed) or 'nothing'}")
        elif args.cmd == "start":
            sd.start(dep.service_name)
            print(f"{dep.service_name} finished successfully")
        elif args.cmd == "status":
            for unit, props in status(dep, sd).items():
                print(unit)
                for k, v in props.items():
                    print(f"  {k}={v}")
    except (NotOwnedError, SystemdError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
