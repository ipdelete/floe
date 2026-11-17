# Floe

A minimal local workflow runner using Python, systemd timers, and journald.
Tasks are Python functions executed sequentially in dependency order. systemd
handles scheduling and process lifecycle; journald stores execution history.
Floe uses only the Python standard library. There are no retries or parallel
execution.

## Requirements

- Linux with systemd and a user systemd session
- Python 3.14 or newer; deployed services also require `/usr/bin/python3` to be 3.14 or newer
- `systemctl`, `journalctl`, and `systemd-analyze` (for deployment calendar validation)
- [uv](https://docs.astral.sh/uv/) to install the `floe` command

```bash
uv sync                # installs the `floe` command into .venv
source .venv/bin/activate   # or prefix commands with `uv run`
```

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
stdout and stderr can share one journal stream, so an unterminated line can be
joined to the next event, which history then cannot recognize.

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

## Deploy with systemd

The unit files are generated from the workflow file; there are no hand-written
units to copy.

```bash
floe install --dry-run examples/workflow.py   # show each unit file and what would change
floe install examples/workflow.py             # write units, daemon-reload, enable timer
floe start mydag                              # run now under systemd, wait for it
floe status mydag                             # systemd's view of service and timer
floe uninstall mydag                          # disable, stop, delete the unit files
```

Every command accepts the workflow file or, once installed, the workflow name.
By default, targets ending in `.py` or containing `/` are files; anything else
is a name. Pass `--name` to explicitly interpret the target as a workflow name,
for example `floe uninstall --name report.py`. This also works for already-installed
names ending in `.py`, even after their definition is deleted.
`floe run mydag` and `floe install mydag` use the file recorded at install
time; `uninstall` by name works even after the file is deleted.

## List installed workflows

```bash
floe list            # name, schedule, timer state, next and last run, file
floe list --check    # also report units that no longer match their file
floe list --json
```

```
NAME   SCHEDULE        TIMER   NEXT                 LAST RUN                       SCRIPT
mydag  *-*-* *:00:00   active  2026-11-16 14:00:00  2026-11-16 13:53:07 succeeded  ~/src/floe/examples/workflow.py
etl    *-*-* 00:00:00  active  2026-11-17 00:00:00  2026-11-16 00:00:01 failed     ~/etl/flow.py
  ! script not found: /home/me/etl/flow.py
```

There is no registry: the units' `[X-Floe]` section records the workflow
name, its definition file, and the Floe source directory the service uses.
`floe list` reports, without running workflow code, a missing unit of the pair,
a missing definition file, units that point at Floe in another location, and
units installed by an older Floe version. `--check` also loads each definition
(running its top-level code) to find units that are out of date, for example
after editing `SCHEDULE` without reinstalling, and then exits nonzero if any
workflow has a problem. Python stdout emitted while loading definitions or their
imports is sent to stderr by the CLI, keeping JSON and table output separate.
The schedule is shown as systemd normalizes it.

`install` writes `<config>/systemd/user/<WORKFLOW>.service` and `.timer`,
where `<config>` is `$XDG_CONFIG_HOME` when set to an absolute path, otherwise
`~/.config`. Files are marked with an `[X-Floe]` section. Installation is
idempotent: when nothing changed it writes nothing and skips `daemon-reload`.
It refuses to overwrite unit files it did not write; use `install --adopt` to
take over existing units of the same name. Masked units, symlinks, and
directories are never overwritten, even with `--adopt`. Both unit files are
planned and checked before either is written. Each replacement is atomic, but
the pair is not a transaction: an unexpected I/O or systemd failure can still
require rerunning `install`.

`uninstall` is best-effort: it attempts every step (disable and stop, remove
files and enable links, `daemon-reload`), then checks that systemd no longer
has the units loaded from Floe's files and that the service is not still
running, resetting a failed state so systemd forgets the unit. It also removes
the timer's last-trigger stamp; otherwise a later install would immediately
"catch up" with a run (`Persistent=true`). Any failures are reported together
with a nonzero exit code. If timer-state cleanup fails, Floe keeps the timer
file as proof of ownership for a retry; if that file was already gone, it writes
a recovery timer with a past-only calendar and no future trigger. `floe list`
identifies this recovery marker and shows the command to finish cleanup. Enable
links are still removed, and other cleanup steps continue. Rerunning the command
continues the interrupted uninstall, including units whose files are gone but
are still loaded. Same-named units loaded from other directories are not touched.

The generated service runs `/usr/bin/python3 -m floe_cli run <path>/workflow.py`
with `PYTHONPATH` pointing at Floe's source directory, the script's directory
as working directory, `TimeoutStartSec=30min` (oneshot services have no start
timeout by default) and `KillMode=control-group`. Because Floe is standard
library only, the service does not need the virtual environment. Moving the
repository or the workflow file requires `install` again.

The service is `Type=oneshot`: activating it while it is already running does
not create an overlapping run. Direct `floe run` invocations are not covered
by that protection.

## Schedule executions

Set `SCHEDULE` to one or more `OnCalendar=` expressions (see
`systemd.time(7)`), then run `floe install <file>` again. For example,
`SCHEDULE = ["*-*-* 08:00:00"]` runs daily at 08:00 local time. Check an
expression with `systemd-analyze calendar "<expression>"`.

Show the next and most recent scheduled execution:

```bash
systemctl --user list-timers mydag.timer --no-pager
```

`Persistent=true` allows a catch-up activation for missed calendar events when
the timer becomes active again. It does not replay every missed occurrence or
wake a sleeping laptop. User services normally depend on your user session;
execution while logged out requires systemd user lingering to be enabled.

Changes to task code take effect on the next execution. Changes to `WORKFLOW`,
`SCHEDULE` or the file's location need `install` again (`floe list --check`
shows which workflows need it). Changing `WORKFLOW`
creates new units: `floe uninstall <old-name>` first.

## Inspect execution history

Every run under systemd (scheduled or `floe start`) is recorded in journald.
Floe reconstructs runs from those records:

```bash
floe runs mydag                       # latest 20 runs, newest first
floe runs mydag --since "24 hours ago" -n 50
floe logs mydag                       # events and output of the latest run
floe logs mydag 4581dde1              # a specific run, by ID or unique prefix
floe runs mydag --json                # also: floe logs mydag --json
```

```
RUN       STARTED              STATUS     DURATION  DETAIL
6904b066  2026-11-16 07:46:18  failed     0.1s      invalid workflow: unknown prerequisites: 'missing'
29b24993  2026-11-16 07:45:25  failed     2.3s      transform: broken input
ca9f4c32  2026-11-16 07:34:38  succeeded  0.0s
```

Runs are ordered by start time (or the earliest available record if the start
was lost). Discovery streams the matching journal history, then fetches complete
records for the selected invocations; delayed records from an older run do not
truncate a newer run. `--since` selects runs with journal activity in that range,
but their displayed history also includes records from before the cutoff.

Only schema-valid events with timezone-aware timestamps are used to reconstruct
runs. Other JSON, including malformed event-shaped output, remains ordinary output.

A run's ID is its systemd invocation ID. Its status is:

- `succeeded` / `failed`: from the run's `run_completed` event. A run that
  failed before starting (for example an invalid definition) is `failed` with
  no tasks, and its error output as the error.
- `running`: no `run_completed` yet and the service is still active.
- `incomplete`: the run started but ended without `run_completed` (killed,
  timed out, crashed), with systemd's result (such as `timeout`) when known.

`floe logs` shows systemd's messages, Floe's events, and task output in order,
with output attributed to the task that was running. Runs of `floe run` outside
systemd are not in the journal and do not appear.

Journald is the only execution-history store. Retention depends on the machine's
journal configuration and storage limits; logs may be rotated or lost across
reboots without persistent journaling. The raw records are also available with
`journalctl --user -u mydag.service` (add `-o json` for structured records, `-f`
to follow).

## Architecture

Floe is a library with user interfaces on top. The `floe` command is one UI; a
web UI is planned and will use the same library.

```
src/floe/        library: all logic, no user I/O
  model.py       Workflow, Event, InstallPlan, UnitChange, UnitState; FloeError hierarchy
  loader.py      load(path) -> Workflow
  runner.py      run(workflow, on_event) -> bool
  deploy.py      Deployment, plan_install/apply/install, uninstall, start, status
  history.py     list_runs, get_run, run_logs -> Run, TaskRun, RunLogs, LogLine
  inventory.py   list_installed, find_installed, check_installed, overview -> InstalledWorkflow, WorkflowSummary
  render.py      unit file text
  store.py       ownership-protected unit files
  systemd.py     thin systemctl wrapper
  journal.py     thin journalctl wrapper
src/floe_cli/    the `floe` command: arguments in, text and exit codes out
```

The rules, enforced by `tests/test_boundaries.py`:

- The library never prints, prompts, exits, parses arguments, or imports a UI.
  It returns data types from `floe.model` (each has `to_dict()` for JSON) or
  raises `FloeError` subclasses with structured fields.
- UIs use only the public API exported by `floe` (`floe.__all__`).
- Everything is standard library only.

Workflow loading and task execution temporarily change Python's process-global
import state. Use them serially, not concurrently with other threads importing
same-named sibling modules.

Use the library directly:

```python
import floe

workflow = floe.load("examples/workflow.py")
floe.run(workflow, on_event=lambda event: print(event.to_dict()))

plan = floe.plan_install(floe.Deployment.for_workflow(workflow), floe.UnitStore(floe.user_unit_dir()))
for change in plan.changes:
    print(change.name, change.action, change.diff())

for past in floe.list_runs("mydag", floe.Journal(), floe.Systemd(), limit=5):
    print(past.run_id, past.status, past.duration, past.failed_task)
```

## Development

```bash
uv run pytest -q
uv run python examples/deploy_demo.py  # validate calendars and plan deployment without contacting the service manager
```

Normal tests use temporary directories and mock the service manager. History
tests use journal records captured from real systemd (`tests/fixtures`). When
available, `systemd-analyze verify` also checks generated units without installing
them. A test runs the exact service command with the system Python and no
virtual environment, and `tests/test_examples.py` keeps the examples working.

To test against your real user systemd session and journald:

```bash
uv run pytest -q -m systemd_e2e --systemd-e2e
```

This is opt-in and takes about 20 seconds. Each test generates its own workflow,
named `floe-e2e-<random>`, in a temporary directory, deploys it with the real
`floe` command, and uninstalls it afterward, verifying that no unit files,
enable links, loaded units, or timer stamps remain. The tests cover deployment
and idempotent reinstall, task output attribution, history and logs of successful, failed, invalid,
timed-out, running, and stopped runs, overlapping starts, schedule drift,
missing definition files, interrupted uninstalls, foreign and masked units, and
the timer starting runs. They execute only their own generated tasks and touch
no other units. Leftover `floe-e2e-*` units from an interrupted session (for
example after SIGKILL) are removed when the tests start, and can be removed by
hand with `floe uninstall <name>`.
