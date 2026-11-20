# Quickstart

Run a sample workflow, put it on a schedule, and check its logs. You'll need Linux, Python 3.14+, `uv`, and a user systemd session with `systemctl`, `systemd-analyze`, and `journalctl`.

## 1. Install Floe

```bash
git clone https://github.com/ipdelete/floe.git
cd floe
uv sync
source .venv/bin/activate
```

Run the rest of the commands from this directory. Keep this checkout and its `.venv` in place while the workflow is installed.

## 2. Run the example

```bash
floe run examples/workflow.py
```

The example is named `mydag`. It prints three messages: fetching, transforming, and publishing. It doesn't fetch or publish anything. Floe writes JSON events to stdout and task messages to stderr.

## 3. Put it on a schedule

```bash
floe install --dry-run examples/workflow.py
floe install examples/workflow.py
floe list
```

The dry run previews the files without writing them. The real install creates a user service and an hourly timer. `floe list` shows when the timer will next start the workflow.

## 4. Start it now and check the logs

```bash
floe start mydag
floe runs mydag
floe logs mydag
```

`floe start` runs the service without waiting for the timer. Look for `succeeded` in `floe runs` and the task messages in `floe logs`. Only runs through systemd show up here. The direct run from step 2 won't appear.

## 5. Remove the schedule

```bash
floe uninstall mydag
```

This removes the service and timer. It leaves the workflow file and existing logs alone.

Next: [write your own workflow](workflows.md) or [change its schedule](deployment.md#schedule-executions).
