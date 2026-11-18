"""The `floe` command: translate arguments into floe library calls and results
into text and exit codes. No workflow or deployment logic lives here."""

import argparse
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
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

    definition = "workflow definition file, or the name of an installed workflow"
    p = sub.add_parser("run", help="run a workflow now in this process, printing JSON events",
                       description="Run sequentially with optional per-task RetryPolicy definitions. "
                                   "Events go to stdout; task output to stderr. No overall timeout outside systemd.")
    _add_target(p, definition)
    p.set_defaults(handler=_run)

    p = sub.add_parser("install", help="write unit files, reload systemd, enable the timer",
                       description="Bind services to this Python environment, which must remain available "
                                   "with Floe and Tenacity. Reinstall after changing environments.")
    _add_target(p, f"{definition} (to reinstall)")
    p.add_argument("--adopt", action="store_true", help="take over existing unit files not written by floe")
    p.add_argument("--dry-run", action="store_true", help="inspect and validate without writes or service-manager calls")
    p.set_defaults(handler=_install)

    target = "workflow definition file or installed workflow name"
    for name, handler, text in [
        ("uninstall", _uninstall, "disable and stop, then delete the unit files"),
        ("start", _start, "run the installed workflow under systemd and wait for it"),
        ("status", _status, "show systemd state of the service and timer"),
    ]:
        p = sub.add_parser(name, help=text)
        _add_target(p, target)
        p.set_defaults(handler=handler)

    p = sub.add_parser("list", help="list installed workflows with schedule and last run")
    p.add_argument("--check", action="store_true",
                   help="also load each definition and report units that are out of date (runs workflow code)")
    p.add_argument("--json", action="store_true", help="print workflows as JSON")
    p.set_defaults(handler=_list)

    p = sub.add_parser("runs", help="list recorded runs under systemd, newest first")
    _add_target(p, target)
    p.add_argument("--since", help='journalctl time, e.g. "24 hours ago", "today", "2026-01-31"')
    p.add_argument("-n", "--limit", type=_positive, default=20, help="number of runs (default: 20)")
    p.add_argument("--json", action="store_true", help="print runs as JSON")
    p.set_defaults(handler=_runs)

    p = sub.add_parser("logs", help="show a run's events and output (default: latest run)")
    _add_target(p, target)
    p.add_argument("run_id", nargs="?", help="run ID or unique prefix")
    p.add_argument("--json", action="store_true", help="print the run and its lines as JSON")
    p.set_defaults(handler=_logs)

    p = sub.add_parser("web", help="serve the read-only dashboard on 127.0.0.1 (Ctrl-C to stop)")
    p.add_argument("--port", type=_port, default=8080, help="local TCP port (default: 8080)")
    p.set_defaults(handler=_web)
    return parser


def _port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return number


def _web(args, ctx) -> int:
    from floe_web import serve

    return serve(ctx.store, ctx.sd, ctx.journal, port=args.port)


def _add_target(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("target", help=help_text)
    parser.add_argument("--name", action="store_true", help="treat TARGET as a workflow name, even if it ends in .py")


def _load(path: Path | str) -> floe.Workflow:
    # Definition and import side effects must not corrupt JSON or event stdout.
    with _stdout_on_stderr():
        return floe.load(path)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _is_path(target: str) -> bool:
    """Targets ending in .py or containing a slash are files; others are names."""
    return target.endswith(".py") or "/" in target


def _workflow_name(target: str, *, name: bool = False) -> str:
    """Resolve a file or name; --name bypasses file-like target detection."""
    if not name and _is_path(target):
        return _load(target).name
    floe.unit_names(target)  # validates the name
    return target


def _definition(target: str, ctx, *, name: bool = False) -> Path:
    """A definition file, given directly or recorded for an installed workflow."""
    if not name and _is_path(target):
        return Path(target)
    installed = floe.find_installed(ctx.store, target)
    if installed.script is None:
        raise floe.NotFoundError(f"no definition file recorded for {target!r}; pass its path")
    return installed.script


def _run(args, ctx) -> int:
    workflow = _load(_definition(args.target, ctx, name=args.name))
    with _stdout_on_stderr() as events:

        def emit(event: floe.Event) -> None:
            # Output a task already wrote belongs before the event that follows it.
            sys.stdout.flush()
            sys.stderr.flush()
            events.write(json.dumps(event.to_dict()) + "\n")
            events.flush()

        return 0 if floe.run(workflow, emit) else 1


@contextmanager
def _stdout_on_stderr() -> Iterator:
    """Route workflow output to stderr, yielding the original stdout for events.

    Covers definition/import code as well as tasks, including child processes
    and raw descriptor writes. Flush before switching and restore both Python
    streams and descriptor 1 even when loading or execution raises.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    saved_fd = os.dup(1)
    events, own_events = sys.stdout, None
    try:
        if _fileno(sys.stdout) == 1:
            # Writes through sys.stdout would follow fd 1 to stderr; keep a copy.
            own_events = open(saved_fd, "w", encoding=sys.stdout.encoding,
                              errors=sys.stdout.errors, closefd=False)
            events = own_events
        os.dup2(2, 1)
        with redirect_stdout(sys.stderr):
            yield events
    finally:
        try:
            sys.stderr.flush()
            if own_events is not None:
                own_events.close()
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)


def _fileno(stream) -> int | None:
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):  # io.UnsupportedOperation is both
        return None


def _install(args, ctx) -> int:
    dep = floe.Deployment.for_workflow(_load(_definition(args.target, ctx, name=args.name)))
    plan = floe.plan_install(dep, ctx.store, adopt=args.adopt)
    if args.dry_run:
        for change in plan.changes:
            reason = f": {change.reason}" if change.reason else ""
            print(f"# {change.path} ({change.action}{reason})\n{change.text}")
        return 1 if plan.conflicts else 0
    changed = floe.apply(plan, ctx.store, ctx.sd)
    # apply always (re-)enables the timer, so report the resulting state, not a change.
    print(f"wrote: {', '.join(changed) or 'nothing (up to date)'}; {plan.timer} is enabled")
    return 0


def _uninstall(args, ctx) -> int:
    removed = floe.uninstall(_workflow_name(args.target, name=args.name), ctx.store, ctx.sd)
    print(f"removed: {', '.join(removed) or 'nothing'}")
    return 0


def _start(args, ctx) -> int:
    name = _workflow_name(args.target, name=args.name)
    try:
        floe.start(name, ctx.sd)
    except floe.SystemdError:
        print(f"error: {name}.service failed; see: floe logs {name}", file=sys.stderr)
        return 1
    print(f"{name}.service finished successfully")
    return 0


def _status(args, ctx) -> int:
    for unit in floe.status(_workflow_name(args.target, name=args.name), ctx.sd).values():
        print(unit.name)
        for key, value in unit.properties.items():
            print(f"  {key}={value}")
    return 0


def _list(args, ctx) -> int:
    with _stdout_on_stderr():
        summaries = floe.overview(ctx.store, ctx.sd, ctx.journal, verify=args.check)
    if args.json:
        print(json.dumps([summary.to_dict() for summary in summaries], indent=2))
    elif not summaries:
        print("no workflows installed")
    else:
        header = ("NAME", "SCHEDULE", "TIMER", "NEXT", "LAST RUN", "SCRIPT")
        rows = []
        for summary in summaries:
            last = summary.last_run
            rows.append((
                summary.name,
                ", ".join(summary.schedule) or "-",
                "active" if summary.active else "inactive",
                _local(summary.next_run),
                "-" if last is None else f"{_local(last.started)} {last.status}",
                _home(summary.workflow.script),
            ))
        heading, *lines = _format_table(header, rows)
        print(heading)
        for summary, line in zip(summaries, lines, strict=True):
            print(line)
            for problem in summary.problems:
                print(f"  ! {problem}")
    return 1 if args.check and any(summary.problems for summary in summaries) else 0


def _home(path: Path | None) -> str:
    if path is None:
        return "?"
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def _runs(args, ctx) -> int:
    name = _workflow_name(args.target, name=args.name)
    _note_if_not_installed(name, ctx)
    runs = floe.list_runs(name, ctx.journal, ctx.sd, since=args.since, limit=args.limit)
    if args.json:
        print(json.dumps([run.to_dict() for run in runs], indent=2))
        return 0
    if not runs:
        print("no runs recorded")
        return 0
    header = ("RUN", "STARTED", "STATUS", "DURATION", "DETAIL")
    rows = [(run.run_id[:8], _local(run.started), run.status, _duration(run.duration), _detail(run))
            for run in runs]
    for line in _format_table(header, rows):
        print(line)
    return 0


def _logs(args, ctx) -> int:
    name = _workflow_name(args.target, name=args.name)
    _note_if_not_installed(name, ctx)
    logs = floe.run_logs(name, args.run_id, ctx.journal, ctx.sd)
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


def _note_if_not_installed(name: str, ctx) -> None:
    """History outlives uninstalling, so these commands still work; say so on
    stderr, so a mistyped name is not mistaken for an empty history."""
    try:
        floe.find_installed(ctx.store, name)
    except floe.NotFoundError:
        print(f"note: workflow {name!r} is not installed; showing recorded history only", file=sys.stderr)
    except OSError:
        pass  # unknown installation state: claim nothing either way


def _detail(run: floe.Run) -> str:
    task = run.failed_task
    if run.status == "running":
        current = next((t for t in run.tasks if t.status == "running"), None)
        if current:
            if current.retry_at:
                return f"{current.name}: waiting to retry at {_local(current.retry_at)}"
            suffix = (f" attempt {current.attempts[-1].number}/{current.max_attempts}"
                      if current.attempts else "")
            return f"running {current.name}{suffix}"
        return ""
    if task and run.status != "succeeded":
        reason = f" ({task.failure_reason})" if task.failure_reason else ""
        return f"{task.name}: {task.error or task.status}{reason}"
    if run.status != "succeeded" and run.error:
        return " ".join(line.strip().removeprefix("- ") for line in run.error.splitlines())
    return "; ".join(f"{t.name}: succeeded after {t.attempts[-1].number} attempts"
                     for t in run.tasks if t.status == "succeeded" and t.attempts
                     and t.attempts[-1].number > 1)


def _describe(line: floe.LogLine) -> str:
    event = line.event
    if event is None:
        label = f"{line.task} attempt {line.attempt}" if line.attempt else line.task
        prefix = f"[{label}] " if label else ""
        return f"systemd: {line.message}" if line.kind == "systemd" else f"{prefix}{line.message}"
    if event.event == "run_started":
        return "▶ run started"
    if event.event == "run_completed":
        return f"■ run {event.status}" + (f": {event.error}" if event.error else "")
    if event.event == "task_started":
        return f"▶ {event.task} started"
    if event.event == "retry_scheduled":
        return f"↻ {event.task}: waiting {event.delay_seconds:g}s before attempt {event.attempt + 1}/{event.max_attempts}"
    label = event.task
    if event.attempt:
        label += f" attempt {event.attempt}/{event.max_attempts}"
    if event.event == "attempt_started":
        return f"▶ {label} started"
    reason = f" ({event.failure_reason})" if event.failure_reason else ""
    return f"■ {label} {event.status}{reason}" + (f": {event.error}" if event.error else "")


def _format_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    """Align all but the last column, including the header in column widths."""
    all_rows = [header, *rows]
    widths = [max(len(row[column]) for row in all_rows) for column in range(len(header) - 1)]
    return [
        ("  ".join(cell.ljust(width) for cell, width in zip(row[:-1], widths, strict=True))
         + "  " + row[-1]).rstrip()
        for row in all_rows
    ]


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
