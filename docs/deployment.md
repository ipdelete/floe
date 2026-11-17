# Deployment

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

`floe status` fails with exit code 1 when systemd knows neither the service
nor the timer, so a mistyped name is reported rather than shown as inactive.
`floe runs` and `floe logs` keep working for names that are no longer
installed, because journald retains their history; they then print a note on
stderr, leaving stdout and exit codes unchanged.

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
by that protection, so a direct run of an installed workflow can overlap a
scheduled one. For workflows that keep state between runs, use `floe start`,
which joins a run already in progress instead of starting a second one.

### The service environment

A service is not started from your shell, so it may not find what your shell
finds. Under systemd:

- `PATH` is the user manager's, typically only system directories. Tools
  installed through version managers or shell profiles (nvm, pyenv,
  `~/.local/bin`) are not found: use absolute paths, or extend `PATH` for the
  child processes a task starts.
- Variables exported by your shell profile, such as API keys, are not set.
  Keep credentials in files or in a tool's own credential store.
- `HOME`, `USER`, `XDG_RUNTIME_DIR` and the session bus
  (`DBUS_SESSION_BUS_ADDRESS`) are set. The locale can differ from your shell's,
  for example `LANG=C.UTF-8`. There is no terminal.
- Python is `/usr/bin/python3` without a virtual environment: workflow code can
  import the standard library, modules next to the workflow file, and packages
  installed for the system Python.

`systemctl --user show-environment` shows the environment services start
from. To try a workflow in that environment before installing it, run it as a
transient unit. This executes its tasks, like `floe run`; `PYTHONPATH` is the
`Floe=` directory shown by `floe install --dry-run`:

```bash
systemd-run --user --wait --pipe --collect \
    -p WorkingDirectory="$PWD" -E PYTHONPATH="$HOME/src/floe/src" \
    /usr/bin/python3 -m floe_cli run "$PWD/workflow.py"
```

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

To see a schedule start a run without waiting for it, temporarily add a
one-off time a few minutes ahead, reinstall, and watch `floe runs`:

```python
SCHEDULE = ["*-*-* 08:00:00", "2026-11-17 13:12:00"]  # second entry: temporary test
```

Adding a future time does not start a run by itself. systemd's default timer
accuracy (`AccuracySec=1min`) lets the run start up to a minute after the
scheduled time. Remove the entry and run `floe install` again afterwards.
`floe start` runs the service immediately, but does not exercise the timer.
