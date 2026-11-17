# Workflows

## Define a workflow

A workflow is a plain Python file containing only definitions. See
`examples/workflow.py`:

```python
import sys

WORKFLOW = "mydag"
SCHEDULE = ["hourly"]  # OnCalendar= expressions, see systemd.time(7)


def fetch():
    print("Fetching data", file=sys.stderr)


def transform():
    print("Transforming data", file=sys.stderr)


tasks = {
    "fetch": fetch,
    "transform": transform,
}

dag = {
    "fetch": set(),
    "transform": {"fetch"},
}
```

`WORKFLOW`, `tasks` and `dag` are required. `SCHEDULE` is optional for running
directly and required for installing. Modules next to the file are importable,
including from inside tasks. Each load keeps its own sibling-module cache, so
separate workflows can use the same helper-module names without sharing code
or state.

Every task must appear in the DAG, including tasks with no prerequisites.
Task names must be nonempty strings. Workflow names must be 1–247 ASCII
characters, start with a letter or digit, and contain only letters, digits,
`_`, `.` or `-`; they are also the systemd unit names. Floe validates the whole
definition when loading it and reports every problem at once (missing or
unknown tasks, cycles, non-callable tasks, bad names or malformed schedule
values), before any task runs. When planning an installation, including
`install --dry-run`, Floe also parses every calendar expression with
`systemd-analyze` before either unit can be written. Direct execution does not
need systemd tools.

Tasks run one at a time. An exception stops the workflow, records failure, and
returns a nonzero exit code. Tasks following the failure are not executed,
including independent tasks later in the execution order.

stdout carries only Floe's events. While tasks run, `floe run` sends anything
they write to stdout to stderr instead, whether from `print()` or from child
processes, and flushes Python's output before each event so that output stays
with the task that wrote it. End output lines with a newline: under systemd,
stdout and stderr share one journal stream, so an unterminated line is joined
to the next event, which history then cannot recognize.

## Run directly

```bash
floe run examples/workflow.py
```

Events are printed to stdout as JSON lines and task output to stderr. Direct
execution does not store these events in journald.

Each run emits `run_started`, `task_started`, `task_completed`, and
`run_completed` events. Every event includes a schema version, workflow name,
UTC timestamp, and run ID. The run ID uses systemd's `INVOCATION_ID` when
available, otherwise a generated UUID. An invalid definition fails before
`run_started`, so it produces an error message but no events.

While a run executes, the environment variable `FLOE_RUN_ID` holds its run ID,
for tasks and the child processes they start. Use it to label files or records
that a run produces; under systemd it matches the ID that `floe runs` shows.

## Write workflows that run unattended

Scheduled runs happen while nobody watches, and Floe never retries. These
patterns keep such workflows predictable:

- **Pass data through files.** Tasks take no arguments and return nothing.
  Write their results to files, for example in a `state/` directory next to the
  workflow file, and replace each file atomically (write a temporary file in the
  same directory, then `os.replace` it) so a crash never leaves half a file.
- **The next run is the retry.** After a failure, the next scheduled run starts
  from the beginning. Make tasks safe to repeat: record what was done, keyed by
  a stable identity such as an item ID, and skip it next time.
- **Save results before acting on them.** When computing a result is expensive
  or not repeatable, store it first; a run that fails while acting then leaves
  the next run something to act on instead of computing it again.
- **Check prerequisites first.** A task that checks required tools and
  credentials, with the tasks that need them depending on it, fails clearly
  instead of halfway through.
- **Finish independent work, then fail.** When one of several independent
  sources fails, record the failure, process the rest, and raise in a final task
  so that `floe runs` shows the run as failed, with the reason.
- **Bound external calls.** Give network requests and child processes their
  own timeouts; the service's `TimeoutStartSec=30min` stops the whole run.
- **Label what a run produces** with `FLOE_RUN_ID`, so files and records can
  be traced back to `floe runs` and `floe logs`.

`examples/checksums/` applies these patterns: it reports files added,
changed, or removed in a directory since the last run, keeps going when a file
cannot be read, and rejects state written by an overlapping run.
