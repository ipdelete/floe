"""The `floe` command: translate arguments into floe library calls and results
into text and exit codes. No workflow or deployment logic lives here."""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import floe
from floe import FloeError, UninstallError, UnitStore, ValidationError

__all__ = ["main"]


def main(argv: list[str] | None = None, *, store: UnitStore | None = None,
         sd: floe.Systemd | None = None, journal: floe.Journal | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args, _Context(store, sd, journal))
    except UninstallError as exc:
        print(f"removed: {', '.join(exc.removed) or 'nothing'}")
        print(f"error: {exc}\nrerun uninstall to retry", file=sys.stderr)
    except ValidationError as exc:
        if exc.subject:
            print(f"error: invalid {exc.subject}:" + "".join(f"\n  - {p}" for p in exc.problems), file=sys.stderr)
        else:
            print("error: " + "\nerror: ".join(exc.problems), file=sys.stderr)
    except (FloeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    return 1


class _Context:
    """Creates the real store and systemd client only when a command needs them."""

    def __init__(self, store, sd, journal):
        self._store, self._sd, self._journal = store, sd, journal

    @property
    def store(self) -> UnitStore:
        if self._store is None:
            self._store = UnitStore(floe.user_unit_dir())
        return self._store

    @property
    def sd(self) -> floe.Systemd:
        if self._sd is None:
            self._sd = floe.Systemd()
        return self._sd

    @property
    def journal(self) -> floe.Journal:
        if self._journal is None:
            self._journal = floe.Journal()
        return self._journal


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="floe", description="Run and schedule local Python workflows.")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p = sub.add_parser("run", help="run a workflow now in this process, printing JSON events")
    p.add_argument("path", type=Path, help="workflow definition file")
    p.set_defaults(handler=_run)

    p = sub.add_parser("install", help="write unit files, reload systemd, enable the timer")
    p.add_argument("path", type=Path, help="workflow definition file")
    p.add_argument("--adopt", action="store_true", help="take over existing unit files not written by floe")
    p.add_argument("--dry-run", action="store_true", help="show what would change and do nothing")
    p.set_defaults(handler=_install)

    target = "workflow definition file or installed workflow name"
    for name, handler, text in [
        ("uninstall", _uninstall, "disable and stop, then delete the unit files"),
        ("start", _start, "run the installed workflow under systemd and wait for it"),
        ("status", _status, "show systemd state of the service and timer"),
    ]:
        p = sub.add_parser(name, help=text)
        p.add_argument("target", help=target)
        p.set_defaults(handler=handler)

    p = sub.add_parser("runs", help="list recorded runs under systemd, newest first")
    p.add_argument("target", help=target)
    p.add_argument("--since", help='journalctl time, e.g. "24 hours ago", "today", "2026-01-31"')
    p.add_argument("-n", "--limit", type=_positive, default=20, help="number of runs (default: 20)")
    p.add_argument("--json", action="store_true", help="print runs as JSON")
    p.set_defaults(handler=_runs)

    p = sub.add_parser("logs", help="show a run's events and output (default: latest run)")
    p.add_argument("target", help=target)
    p.add_argument("run_id", nargs="?", help="run ID or unique prefix")
    p.add_argument("--json", action="store_true", help="print the run and its lines as JSON")
    p.set_defaults(handler=_logs)
    return parser


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _workflow_name(target: str) -> str:
    """A path to a definition file, or a workflow name (e.g. after deleting the file)."""
    path = Path(target)
    if path.suffix == ".py" or path.is_file():
        return floe.load(path).name
    floe.unit_names(target)  # validates the name
    return target


def _run(args, ctx) -> int:
    workflow = floe.load(args.path)

    def emit(event: floe.Event) -> None:
        print(json.dumps(event.to_dict()), flush=True)

    return 0 if floe.run(workflow, emit) else 1


def _install(args, ctx) -> int:
    dep = floe.Deployment.for_workflow(floe.load(args.path))
    plan = floe.plan_install(dep, ctx.store, adopt=args.adopt)
    if args.dry_run:
        for change in plan.changes:
            reason = f": {change.reason}" if change.reason else ""
            print(f"# {change.path} ({change.action}{reason})\n{change.text}")
        return 1 if plan.conflicts else 0
    changed = floe.apply(plan, ctx.store, ctx.sd)
    print(f"wrote: {', '.join(changed) or 'nothing (up to date)'}; enabled {plan.timer}")
    return 0


def _uninstall(args, ctx) -> int:
    removed = floe.uninstall(_workflow_name(args.target), ctx.store, ctx.sd)
    print(f"removed: {', '.join(removed) or 'nothing'}")
    return 0


def _start(args, ctx) -> int:
    name = _workflow_name(args.target)
    try:
        floe.start(name, ctx.sd)
    except floe.SystemdError:
        print(f"error: {name}.service failed; see: floe logs {name}", file=sys.stderr)
        return 1
    print(f"{name}.service finished successfully")
    return 0


def _status(args, ctx) -> int:
    for unit in floe.status(_workflow_name(args.target), ctx.sd).values():
        print(unit.name)
        for key, value in unit.properties.items():
            print(f"  {key}={value}")
    return 0


def _runs(args, ctx) -> int:
    runs = floe.list_runs(_workflow_name(args.target), ctx.journal, ctx.sd, since=args.since, limit=args.limit)
    if args.json:
        print(json.dumps([run.to_dict() for run in runs], indent=2))
        return 0
    if not runs:
        print("no runs recorded")
        return 0
    rows = [("RUN", "STARTED", "STATUS", "DURATION", "DETAIL")]
    for run in runs:
        rows.append((run.run_id[:8], _local(run.started), run.status, _duration(run.duration), _detail(run)))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    for row in rows:
        print(("  ".join(cell.ljust(width) for cell, width in zip(row[:4], widths)) + "  " + row[4]).rstrip())
    return 0


def _logs(args, ctx) -> int:
    logs = floe.run_logs(_workflow_name(args.target), args.run_id, ctx.journal, ctx.sd)
    if args.json:
        print(json.dumps(logs.to_dict(), indent=2))
        return 0
    run = logs.run
    print(f"run {run.run_id}  {run.status}  started {_local(run.started)}  duration {_duration(run.duration)}")
    if run.error:
        print(f"error: {run.error}")
    for line in logs.lines:
        when = line.timestamp.astimezone().strftime("%H:%M:%S.%f")[:-3]
        print(f"{when}  {_describe(line)}")
    return 0


def _detail(run: floe.Run) -> str:
    task = run.failed_task
    if run.status == "running":
        current = next((t.name for t in run.tasks if t.status == "running"), None)
        return f"running {current}" if current else ""
    if task and run.status != "succeeded":
        return f"{task.name}: {task.error or task.status}"
    if run.status != "succeeded" and run.error:
        return " ".join(line.strip().removeprefix("- ") for line in run.error.splitlines())
    return ""


def _describe(line: floe.LogLine) -> str:
    event = line.event
    if event is None:
        prefix = f"[{line.task}] " if line.task else ""
        return f"systemd: {line.message}" if line.kind == "systemd" else f"{prefix}{line.message}"
    if event.event == "run_started":
        return "▶ run started"
    if event.event == "run_completed":
        return f"■ run {event.status}" + (f": {event.error}" if event.error else "")
    if event.event == "task_started":
        return f"▶ {event.task} started"
    return f"■ {event.task} {event.status}" + (f": {event.error}" if event.error else "")


def _local(value: datetime | None) -> str:
    return "-" if value is None else value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _duration(value: timedelta | None) -> str:
    if value is None:
        return "-"
    seconds = value.total_seconds()
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"
