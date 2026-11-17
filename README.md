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

## Quickstart

```bash
floe run examples/workflow.py        # run directly: events on stdout, task output on stderr
floe install examples/workflow.py    # write a systemd service and timer for workflow "mydag"
floe start mydag                     # run it under systemd now
floe runs mydag                      # its history, from journald
floe logs mydag                      # events and output of the latest run
floe web                             # read-only dashboard at http://127.0.0.1:8080/
floe uninstall mydag
```

The [quickstart](docs/quickstart.md) walks through writing a workflow,
installing it, and watching its timer start a run.

## Documentation

- [Quickstart](docs/quickstart.md): from a first workflow to scheduled runs
- [Workflows](docs/workflows.md): define, run, and write workflows that run unattended
- [Deployment](docs/deployment.md): install, list, uninstall, the service environment, schedules
- [History](docs/history.md): runs and logs from journald
- [Dashboard](docs/web.md): minimal read-only local web UI
- [Library](docs/library.md): architecture and the Python API
- [Development](docs/development.md): tests and the real-systemd suite
