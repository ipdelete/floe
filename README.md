# Floe

A minimal local workflow runner using Python, systemd timers, and journald.
Tasks are Python functions executed sequentially in dependency order. systemd
handles scheduling and process lifecycle; journald stores execution history.
Both the runner and deployment commands use only the Python standard library.
There are no retries or parallel execution.

## Requirements

- Linux with systemd and a user systemd session
- Python 3.14 or newer, as specified by this repository
- `systemctl` and `journalctl`
- [uv](https://docs.astral.sh/uv/) for the deployment commands

## Define a workflow

Edit `workflow.py`. Add your task functions to `tasks` and declare their
prerequisites in `dag`:

```python
def fetch():
    # Fetch your data here.
    pass


def transform():
    # Transform the fetched data here.
    pass


tasks = {
    "fetch": fetch,
    "transform": transform,
}

dag = {
    "fetch": set(),
    "transform": {"fetch"},
}
```

Every task must appear in the DAG, including tasks with no prerequisites.
Task names must be nonempty strings and must match the DAG entries. The runner
checks that tasks are callable and rejects missing entries, unknown prerequisites,
and cycles before executing any task.

Tasks run one at a time. An exception stops the workflow, records failure, and
returns a nonzero exit code. Tasks following the failure are not executed,
including independent tasks later in the execution order.

Set `WORKFLOW` for the workflow name (also the systemd unit name) and
`SCHEDULE` for when it runs. Workflow names must be 1–247 ASCII characters,
start with a letter or digit, and contain only letters, digits, `_`, `.` or `-`.
The schedule must contain at least one nonempty `OnCalendar=` expression.
Send ordinary task messages to stderr so stdout remains newline-delimited JSON:

```python
print("Fetching data", file=sys.stderr)
```

## Run directly

From the repository directory:

```bash
python3 workflow.py        # or: python3 workflow.py run
```

Events are printed to stdout and task messages to stderr. Direct execution does
not automatically store these events in journald.

Each invocation emits `run_started`, `task_started`, `task_completed`, and
`run_completed` events. Every event includes a schema version, workflow name,
UTC timestamp, and run ID. The run ID uses systemd's `INVOCATION_ID` when
available, otherwise a generated UUID.

## Deploy with systemd

The unit files are generated from `workflow.py`; there are no hand-written
units to copy. Deployment commands need the local `floe` package, so run them
through `uv` (there are no third-party runtime dependencies):

```bash
uv run python workflow.py install --dry-run   # print the unit files, change nothing
uv run python workflow.py install             # write units, daemon-reload, enable timer
uv run python workflow.py start               # run now under systemd, wait for it
uv run python workflow.py status              # systemd's view of service and timer
uv run python workflow.py uninstall           # disable, stop, delete the unit files
```

`install` writes `<config>/systemd/user/<WORKFLOW>.service` and `.timer`,
where `<config>` is `$XDG_CONFIG_HOME` when set to an absolute path, otherwise
`~/.config`. Files are marked with an `[X-Floe]` section. Installation is
idempotent: when nothing changed it writes nothing and skips `daemon-reload`.
It refuses to overwrite unit files
it did not write; use `install --adopt` to take over existing units of the same
name (for example ones installed by hand from an earlier version of this README).
Masked units, symlinks, and directories are never overwritten, even with `--adopt`.
`uninstall` is best-effort: it attempts every step (disable and stop, remove
files and enable links, `daemon-reload`), then checks that systemd no longer
has the units loaded from Floe's files and that the service is not still
running. Any failures are reported together with a nonzero exit code. Rerunning
it continues an interrupted uninstall, including units whose files are gone but
are still loaded. Same-named units loaded from other directories are not touched.
Both unit files are rendered and their destinations checked before either is
written. Each replacement is atomic, but the pair is not a transaction: an
unexpected I/O or systemd failure can still require rerunning `install`.

The generated service runs `/usr/bin/python3 <path>/workflow.py run` with the
script's directory as working directory, `TimeoutStartSec=30min` (oneshot
services have no start timeout by default) and `KillMode=control-group`. The
runner itself uses only the standard library, so the service does not need the
virtual environment.

The service is `Type=oneshot`: activating it while it is already running does
not create an overlapping run. Direct invocations of the script are not covered
by that protection.

## Schedule executions

Set `SCHEDULE` in `workflow.py` to one or more `OnCalendar=` expressions (see
`systemd.time(7)`), then run `uv run python workflow.py install` again. For
example, `SCHEDULE = ["*-*-* 08:00:00"]` runs daily at 08:00 local time.
Check an expression with `systemd-analyze calendar "<expression>"`.

Show the next and most recent scheduled execution:

```bash
systemctl --user list-timers mydag.timer --no-pager
```

`Persistent=true` allows a catch-up activation for missed calendar events when
the timer becomes active again. It does not replay every missed occurrence or
wake a sleeping laptop. User services normally depend on your user session;
execution while logged out requires systemd user lingering to be enabled.

Changes to task code in `workflow.py` take effect on the next execution.
Changes to `WORKFLOW`, `SCHEDULE` or the script's location need `install` again.
Changing `WORKFLOW` creates new units: `uninstall` with the old name first.

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

Inspect the service's current status:

```bash
systemctl --user status mydag.service --no-pager
```

Journald is the only execution-history store. Retention depends on the machine's
journal configuration and storage limits; logs may be rotated or lost across
reboots without persistent journaling. A forcibly terminated workflow may have
no `run_completed` event.

## Development

```bash
uv run pytest -q
uv run python examples/deploy_demo.py  # render units without writing or calling systemd
```

Normal tests use temporary directories and mock the service manager. When
available, `systemd-analyze verify` also checks generated units without installing
them.

To test the real deployment lifecycle in your user systemd session:

```bash
uv run pytest -q -m systemd_e2e --systemd-e2e
```

**This is opt-in and uses disposable units in your real user systemd session.**
It uninstalls any existing owned workflow units, verifies cleanup, deploys them,
checks idempotency, runs the actual tasks, and checks their JSON events in journald.
A `finally` block uninstalls the service and timer and verifies that the units and
enable links are gone, even if a test assertion fails. There are no backups or
restored installations. It refuses foreign or masked units and an existing
service pointing at a different script; unrelated units are untouched. Run it
only when it is safe to interrupt and execute the tasks in `workflow.py`. Like any
cleanup code, it cannot guarantee removal after SIGKILL or a systemd failure;
`uv run python workflow.py uninstall` can clean up an interrupted test.

Deployment is intentionally limited to generating Floe's service and timer.
The former internal `floe.units` API (Pydantic models, generic unit parsing, and
`UnitDir`) has been removed. Use `Deployment.render_units()` to inspect generated
text; Floe does not provide general-purpose editing of existing systemd units.
There is no standalone `floe` command: use the commands on `workflow.py` above.
