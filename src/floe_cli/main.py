"""The `floe` command: translate arguments into floe library calls and results
into text and exit codes. No workflow or deployment logic lives here."""

import argparse
import json
import sys
from pathlib import Path

import floe
from floe import FloeError, UninstallError, UnitStore, ValidationError

__all__ = ["main"]


def main(argv: list[str] | None = None, *, store: UnitStore | None = None,
         sd: floe.Systemd | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args, _Context(store, sd))
    except UninstallError as exc:
        print(f"removed: {', '.join(exc.removed) or 'nothing'}")
        print(f"error: {exc}\nrerun uninstall to retry", file=sys.stderr)
    except ValidationError as exc:
        print("error: invalid workflow:" + "".join(f"\n  - {p}" for p in exc.problems), file=sys.stderr)
    except (FloeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    return 1


class _Context:
    """Creates the real store and systemd client only when a command needs them."""

    def __init__(self, store, sd):
        self._store, self._sd = store, sd

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
    return parser


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
    floe.start(name, ctx.sd)
    print(f"{name}.service finished successfully")
    return 0


def _status(args, ctx) -> int:
    for unit in floe.status(_workflow_name(args.target), ctx.sd).values():
        print(unit.name)
        for key, value in unit.properties.items():
            print(f"  {key}={value}")
    return 0
