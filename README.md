# Floe

A minimal local workflow runner using Python, systemd timers, and journald.
Tasks are Python functions executed sequentially in dependency order. systemd
handles scheduling and process lifecycle; journald stores execution history.
Floe uses only the Python standard library. There are no retries or parallel
execution.

## Requirements

- Linux with systemd and a user systemd session
- Python 3.14 or newer, as specified by this repository
- `systemctl` and `journalctl`
- [uv](https://docs.astral.sh/uv/) to install the `floe` command

```bash
uv sync                # installs the `floe` command into .venv
source .venv/bin/activate   # or prefix commands with `uv run`
```

## Define a workflow

A workflow is a plain Python file containing only definitions. See
`workflow.py`:

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
directly and required for installing. Modules next to the file are importable.

Every task must appear in the DAG, including tasks with no prerequisites.
Task names must be nonempty strings. Workflow names must be 1–247 ASCII
characters, start with a letter or digit, and contain only letters, digits,
`_`, `.` or `-`; they are also the systemd unit names. Floe validates the whole
definition when loading it and reports every problem at once (missing or
unknown tasks, cycles, non-callable tasks, bad names or schedules), before any
task runs or any unit is installed.

Tasks run one at a time. An exception stops the workflow, records failure, and
returns a nonzero exit code. Tasks following the failure are not executed,
including independent tasks later in the execution order. Send task messages
to stderr so stdout remains newline-delimited JSON.

## Run directly

```bash
floe run workflow.py
```

Events are printed to stdout as JSON lines and task messages to stderr. Direct
execution does not store these events in journald.

Each run emits `run_started`, `task_started`, `task_completed`, and
`run_completed` events. Every event includes a schema version, workflow name,
UTC timestamp, and run ID. The run ID uses systemd's `INVOCATION_ID` when
available, otherwise a generated UUID. An invalid definition fails before
`run_started`, so it produces an error message but no events.

## Deploy with systemd

The unit files are generated from the workflow file; there are no hand-written
units to copy.

```bash
floe install --dry-run workflow.py   # show each unit file and what would change
floe install workflow.py             # write units, daemon-reload, enable timer
floe start mydag                     # run now under systemd, wait for it
floe status mydag                    # systemd's view of service and timer
floe uninstall mydag                 # disable, stop, delete the unit files
```

`start`, `status` and `uninstall` accept either the workflow file or the
workflow name, so a workflow can be uninstalled after its file is deleted.

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
running. Any failures are reported together with a nonzero exit code. Rerunning
it continues an interrupted uninstall, including units whose files are gone but
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
`systemd.time(7)`), then run `floe install workflow.py` again. For example,
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
`SCHEDULE` or the file's location need `install` again. Changing `WORKFLOW`
creates new units: `floe uninstall <old-name>` first.

## Inspect execution history

View recent workflow events and task output:

```bash
journalctl --user -u mydag.service --since "24 hours ago" --no-pager
```

Follow logs as executions occur:

```bash
journalctl --user -u mydag.service -f
```

Read journal records as JSON:

```bash
journalctl --user -u mydag.service --since "24 hours ago" -o json --no-pager
```

With `-o json`, each line is a journal record. Workflow event JSON is contained
in its `MESSAGE` field; ordinary task output and systemd lifecycle messages are
also present. Use the event's `run_id` to distinguish executions.

Journald is the only execution-history store. Retention depends on the machine's
journal configuration and storage limits; logs may be rotated or lost across
reboots without persistent journaling. A forcibly terminated workflow may have
no `run_completed` event.

## Architecture

Floe is a library with user interfaces on top. The `floe` command is one UI; a
web UI is planned and will use the same library.

```
src/floe/        library: all logic, no user I/O
  model.py       Workflow, Event, InstallPlan, UnitChange, UnitState; FloeError hierarchy
  loader.py      load(path) -> Workflow
  runner.py      run(workflow, on_event) -> bool
  deploy.py      Deployment, plan_install/apply/install, uninstall, start, status
  render.py      unit file text
  store.py       ownership-protected unit files
  systemd.py     thin systemctl wrapper
src/floe_cli/    the `floe` command: arguments in, text and exit codes out
```

The rules, enforced by `tests/test_boundaries.py`:

- The library never prints, prompts, exits, parses arguments, or imports a UI.
  It returns data types from `floe.model` (each has `to_dict()` for JSON) or
  raises `FloeError` subclasses with structured fields.
- UIs use only the public API exported by `floe` (`floe.__all__`).
- Everything is standard library only.

Use the library directly:

```python
import floe

workflow = floe.load("workflow.py")
floe.run(workflow, on_event=lambda event: print(event.to_dict()))

plan = floe.plan_install(floe.Deployment.for_workflow(workflow), floe.UnitStore(floe.user_unit_dir()))
for change in plan.changes:
    print(change.name, change.action, change.diff())
```

## Development

```bash
uv run pytest -q
uv run python examples/deploy_demo.py  # plan a deployment without writing or calling systemd
```

Normal tests use temporary directories and mock the service manager. When
available, `systemd-analyze verify` also checks generated units without installing
them. A test runs the exact service command with the system Python and no
virtual environment.

To test the real deployment lifecycle in your user systemd session:

```bash
uv run pytest -q -m systemd_e2e --systemd-e2e
```

**This is opt-in and uses disposable units in your real user systemd session.**
It runs the `floe` command to uninstall any existing owned workflow units,
verify cleanup, deploy them, check idempotency, run the actual tasks, and check
their JSON events in journald. A `finally` block uninstalls the service and
timer and verifies that the units and enable links are gone, even if a test
assertion or the uninstall itself fails. There are no backups or restored
installations. It refuses foreign or masked units and an existing service
pointing at a different script; unrelated units are untouched. Run it only when
it is safe to interrupt and execute the tasks in `workflow.py`. Like any cleanup
code, it cannot guarantee removal after SIGKILL or a systemd failure;
`floe uninstall mydag` can clean up an interrupted test.
