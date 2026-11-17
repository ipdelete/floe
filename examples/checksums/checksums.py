"""Report files that were added, changed, or removed in a directory since the last run.

A workflow meant to run unattended (see "Write workflows that run unattended"
in the README):

- tasks pass data through atomically replaced JSON files in ./state, tagged
  with FLOE_RUN_ID so a task never reads another run's results;
- the baseline of known checksums is only replaced once changes are reported,
  so after a failure the next run reports them again rather than losing them;
- a file that cannot be read does not stop the others: it is recorded, the
  remaining files are processed, and the last task fails the run.

Point WATCH at the directory to watch, then:

    floe run examples/checksums/checksums.py
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from statefiles import read_json, sha256, write_json

WORKFLOW = "checksums"
SCHEDULE = ["daily"]

BASE = Path(__file__).resolve().parent
WATCH = BASE / "watched"  # the directory to watch
STATE = BASE / "state"
REPORT = BASE / "out" / "changes.log"


def log(message: str) -> None:
    print(message, file=sys.stderr)


def this_run(name: str) -> dict:
    """A state file written earlier in this run; never a stale one from another run."""
    data = read_json(STATE / name, {})
    if data.get("run_id") != os.environ["FLOE_RUN_ID"]:
        raise RuntimeError(f"state/{name} was not written by this run")
    return data


def preflight():
    if not WATCH.is_dir():
        raise RuntimeError(f"directory to watch not found: {WATCH}")


def scan():
    files, errors = {}, {}
    for path in sorted(WATCH.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        name = str(path.relative_to(WATCH))
        try:
            files[name] = sha256(path)
        except OSError as exc:
            errors[name] = exc.strerror or str(exc)
            log(f"cannot read {name}: {errors[name]}")
    write_json(STATE / "scan.json", {"run_id": os.environ["FLOE_RUN_ID"], "files": files, "errors": errors})
    log(f"scanned {len(files)} files")


def compare():
    scanned = this_run("scan.json")
    known = read_json(STATE / "known.json", {})
    files, unreadable = scanned["files"], set(scanned["errors"])
    changes = {
        "added": sorted(set(files) - set(known)),
        "changed": sorted(name for name in files.keys() & known.keys() if files[name] != known[name]),
        # A file that exists but could not be read is unknown, not removed.
        "removed": sorted(set(known) - set(files) - unreadable),
    }
    write_json(STATE / "changes.json", {"run_id": scanned["run_id"], **changes})
    log(", ".join(f"{len(names)} {kind}" for kind, names in changes.items()))


def report():
    changes = this_run("changes.json")
    when = datetime.now(UTC).isoformat(timespec="seconds")
    lines = [f"{when} {changes['run_id'][:8]} {kind} {name}\n"
             for kind in ("added", "changed", "removed") for name in changes[kind]]
    if lines:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        with REPORT.open("a") as file:
            file.writelines(lines)
    # Only now accept the scan as the new baseline. Unreadable files keep their
    # previous checksum, so they are compared again on the next run.
    scanned = this_run("scan.json")
    known = read_json(STATE / "known.json", {})
    baseline = {**{name: known[name] for name in scanned["errors"] if name in known}, **scanned["files"]}
    write_json(STATE / "known.json", baseline)
    log(f"reported {len(lines)} changes")


def finish():
    errors = this_run("scan.json")["errors"]
    if errors:
        raise RuntimeError(f"{len(errors)} files could not be read: {', '.join(sorted(errors))}")


tasks = {"preflight": preflight, "scan": scan, "compare": compare, "report": report, "finish": finish}

dag = {
    "preflight": set(),
    "scan": {"preflight"},
    "compare": {"scan"},
    "report": {"compare"},
    "finish": {"report"},
}
