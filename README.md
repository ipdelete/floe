# Floe

A minimal local workflow runner using Python, systemd timers, and journald.
Tasks are Python functions executed sequentially in dependency order. systemd
handles scheduling and process lifecycle; journald stores execution history.
The runner uses only the standard library; the deployment commands use pydantic.
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
Task names and DAG nodes must match. The runner checks for missing tasks and
cycles before executing any task.

Tasks run one at a time. An exception stops the workflow, records failure, and
returns a nonzero exit code. Tasks following the failure are not executed,
including independent tasks later in the execution order.

Set `WORKFLOW` for the workflow name (also the systemd unit name) and
`SCHEDULE` for when it runs.
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
units to copy. Deployment commands need the `floe` package (and pydantic), so
run them through `uv`:

```bash
uv run python workflow.py install --dry-run   # print the unit files, change nothing
uv run python workflow.py install             # write units, daemon-reload, enable timer
uv run python workflow.py start               # run now under systemd, wait for it
uv run python workflow.py status              # systemd's view of service and timer
uv run python workflow.py uninstall           # disable, stop, delete the unit files
```

`install` writes `~/.config/systemd/user/<WORKFLOW>.service` and `.timer`,
marked with an `[X-Floe]` section. It is idempotent: when nothing changed it
writes nothing and skips `daemon-reload`. It refuses to overwrite unit files
it did not write; use `install --adopt` to take over existing units of the same
name (for example ones installed by hand from an earlier version of this README).

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
