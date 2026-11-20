# Deployment

## Deploy with systemd

```bash
floe install --dry-run examples/workflow.py
floe install examples/workflow.py
floe start mydag
floe status mydag
floe uninstall mydag
```

A dry run checks the schedule and timeout and shows what would change. It doesn't write units or contact the service manager. Installation creates a user service and enables its timer. `floe start` runs the service now and waits for it. If the service is already running, `start` waits for that run instead. A direct `floe run` can overlap it.

New installs use one worker and a 30-minute timeout for the whole run, including retry waits. Set a different timeout with `--timeout 5min`, or [enable parallel tasks](workflows.md#run-independent-tasks-in-parallel) with `--workers 2`. Reinstalling keeps those settings unless you override them.

After installation, use the workflow name instead of its file path. For a name that ends in `.py`, pass `--name` to commands that accept either a path or a name: `floe uninstall --name report.py`. `floe cancel` always takes the installed name.

Floe won't replace units it doesn't own unless you pass `--adopt`. Even then, it won't overwrite masks, symlinks, or directories. Uninstall stops the service and removes the timer and unit files, not your Python file or logs. If uninstall reports a failure, fix the problem and rerun it by name, even if the Python file is gone.

## List installed workflows

```bash
floe list          # show schedules, timer state, next run, and latest result
floe list --check  # also check whether units match the workflow file
```

Plain `floe list` never imports workflows. `--check` loads the definitions and runs their top-level Python code, but does not call their tasks. Use it only with trusted files. It exits nonzero if it finds a problem. Both forms support `--json`.

## Schedule executions

```python
SCHEDULE = ["*-*-* 08:00:00"]  # every day at 8 a.m. local time
```

Use `systemd-analyze calendar "*-*-* 08:00:00"` to check an expression. You can list more than one. Run `floe install mydag` again after changing the schedule. Task-only edits take effect on the next run. If you move the file, install from its new path. If you rename the workflow, uninstall the old name first.

User timers normally run only while your user session is active. To run while logged out, enable systemd user lingering. When a timer becomes active again, it can catch up with one run; it doesn't replay every missed time or wake a sleeping machine. `floe start` tests the service, not the timer.

### The service environment

The service runs from the workflow file's directory. It uses the Python interpreter that ran `floe install`, so keep that environment and its packages available. The dry run shows the interpreter path. If you move the checkout or Python environment, reinstall. Don't install from a temporary `uv run --with ...` environment.

A service doesn't run from your shell. It may have a different `PATH`, won't read your shell profile, and has no terminal. Use absolute paths for tools and a file or credential store for secrets. To see the user manager's environment, run `systemctl --user show-environment`.

### Upgrade existing installations

Older units may point to system Python instead of an environment with Floe and Tenacity. Install Floe in a durable Python 3.14+ environment, then run `floe install --dry-run NAME` and `floe install NAME` from that environment for each workflow. Check with `floe list --check`. Restart the dashboard separately if it's running. Reinstalling doesn't erase journal history.
