# Read-only local dashboard

```bash
floe web                 # http://127.0.0.1:8080/
floe web --port 8123      # choose a different local port
```

Open the printed address in a browser. The server runs in the foreground;
Ctrl-C stops it. The address and HTTP access logs go to stderr, not stdout.
An unavailable port or other startup error exits with code 1. Invalid arguments
exit with code 2. A normal Ctrl-C shutdown exits with code 0.

The dashboard requires the same user-session tools as the history CLI:
`systemctl` and `journalctl`. It uses the Python standard library, with no
frontend dependencies, JavaScript, or external assets. It does not launch a
browser, install a service, or change journal retention.

A six-icon Lucide subset marks Refresh, back navigation, and the four run
statuses. Icons render inline beside visible text, inherit the existing colors,
and remain static even for running workflows: the page is a snapshot, not a
live stream. They are hidden from assistive technology and cannot receive focus.
The SVGs and their ISC/MIT notices are bundled under `src/floe_web/icons/`;
no icon package, font, CDN request, or JavaScript is needed. The Floe wordmark
is unchanged.

## Pages

- **Workflows** (`/`): installed workflows, schedule, next execution, latest
  recent run with its start time and duration, timer activity/enablement, and
  installation or query problems. On narrow screens each workflow becomes a
  labeled, stacked row. Timer state is separate from the last run's outcome:
  an active but disabled timer is shown as such; failed queries say unavailable,
  not inactive or disabled.
- **Workflow** (`/workflows/<name>`): schedule and up to 20 recent runs.
- **Run** (`/workflows/<name>/runs/<full-invocation-id>`): outcome-led heading,
  task results, errors, systemd result when available, and chronological events
  and output. Browser titles include the status, workflow, and short invocation
  ID so multiple run tabs remain distinguishable.

Timestamps use the server's local timezone, with the zone and UTC offset
labeled in a compact footer. Calendar interpretation details live in the
**History and display limits** disclosure rather than above every table.
Today/tomorrow labels on the inventory and next-run times
also use the server's local date, not the browser's. Calendar expressions are
shown verbatim: systemd uses local time unless an expression specifies its own
timezone. Each HTML `<time>` retains its precise timestamp and has a full local
timestamp with offset in its title; historical timestamps use their applicable
DST offset. The header shows when the page was rendered.

Navigation uses ordinary links; use **Refresh** or your browser's reload button
to update a page. There is no automatic refresh or live log streaming. Pages
and inventory are cached in memory for up to three seconds; the page cache
retains at most 16 pages. Nothing is cached on disk. A native **History and
display limits** disclosure contains the retention, caching, and query-limit
notes, while actual partial-history warnings remain visible. A keyboard skip
link goes directly to the main content.

Inventory and workflow pages discover runs with journal activity in the last
seven days. "No recent runs" does not mean a workflow has never run. A saved
full run URL can still show older history if journald retains it. Run and
workflow URLs also work after uninstall, subject to available history; an
uninstalled workflow with no recent history has no workflow page.

Each selected run reads at most its newest 1,000 journal records, plus one
record to detect truncation. **Partial history** is explicitly marked: older
tasks, task attribution, and start times may be unavailable. Missing start
times have no duration. A truncated run with no completion evidence is not
assumed successful. Log messages longer than 2,000 characters and errors longer
than 8,000 characters are shortened for display, with a marker. These are record
and display limits, not a byte limit on journal
entries or a fixed wall-clock query deadline. Discovery streams the matching
seven-day window, so a busy journal can still take time to query.

The log view renders lifecycle events as short descriptions, wraps output,
and de-emphasizes systemd messages. Log timestamps show just the time, with
separate date/offset headings when the local date or DST offset changes.
**Raw lifecycle events** expands the original event messages (subject to the
same display limit) using native HTML; no JavaScript is needed. Ordinary output,
including malformed event-shaped JSON, is never reinterpreted as a lifecycle
event. Short positive durations display as `<0.1s` rather than `0.0s`.

Task names in the summary link to their first retained log record, with
keyboard-focusable task markers and **Back to tasks** links. Markers do not
reorder records or imply that a missing start was observed. If a task has no
retained log record, it has no jump link and is labeled accordingly. These
fragments navigate the current bounded view, not stable journal-record
permalinks. The unabridged-log command includes the exact workflow and run ID.

Use `floe logs <name> <id>` or `journalctl` for unabridged logs. The CLI's default
history and log commands retain their existing unbounded-per-run behavior.

Empty views distinguish missing installations from absent recent journal
activity and show copyable terminal commands. Workflow problems offer an
**Inspect in terminal** disclosure. Error pages suggest read-only CLI checks;
a transient detail-query failure retains its validated local route for both
**Try again** and **Refresh**. Invalid or external targets fall back to the
inventory. Retry only reads state; it does not restart a workflow.

## Read-only and local

Browsing never imports workflow definitions, checks definition drift, launches
tasks, or changes units. Names ending in `.py` remain names in web URLs. The UI
uses installed metadata, systemd queries, and journal records only. Use
`floe list --check` separately when you explicitly want definition checks.

The server binds only to IPv4 `127.0.0.1`, accepts matching localhost Host
headers, and has no authentication or remote-listen option. It is intended for
your own machine, not public hosting or reverse-proxy deployment. Other local
users or processes that can connect may read the same dashboard; loopback is
not per-user access control. Logs can contain secrets, so do not expose or
forward the port to untrusted clients.

Workflow names, errors, and logs are escaped as text. The server provides no
write routes, arbitrary file serving, DAG visualization, or definition editor.
`incomplete` remains distinct from `failed`; query failures are shown as
problems or HTTP errors rather than empty successful results. Journald
retention still determines which history exists.
