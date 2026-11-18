# Inspect execution history

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

Only schema-valid events with timezone-aware timestamps and matching workflow
and invocation identities are used to reconstruct runs. Other JSON, including
malformed event-shaped output, remains ordinary output. If a task invokes a
different workflow with `floe run`, that child's events remain output of the
parent task, even when the child inherits the same systemd invocation ID.

A run's ID is its systemd invocation ID. Its status is:

- `succeeded` / `failed`: from the run's `run_completed` event. A run that
  failed before starting (for example an invalid definition) is `failed` with
  no tasks, and its error output as the error.
- `running`: no `run_completed` yet and the service is still active.
- `incomplete`: the run started but ended without `run_completed` (killed,
  timed out, crashed), with systemd's result (such as `timeout`) when known.

`floe logs` shows systemd's messages, Floe's events, and task output in order,
with output attributed to the task that was running. Each line's time is when
journald received it; in `--json`, an event line's `event` also keeps the
event's own timestamp, which run start, finish, and durations are based on.
Runs of `floe run` outside systemd are not in the journal and do not appear.

Journald is the only execution-history store. Retention depends on the machine's
journal configuration and storage limits; logs may be rotated or lost across
reboots without persistent journaling. The raw records are also available with
`journalctl --user -u mydag.service` (add `-o json` for structured records, `-f`
to follow).

The [read-only dashboard](web.md), started with `floe web`, shows the same
history with a seven-day discovery window and a newest-1,000-record limit per
selected run. Partial history is labeled. CLI commands do not apply that
record limit; their JSON run objects include `truncated: false`.
