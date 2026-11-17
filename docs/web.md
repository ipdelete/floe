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

## Pages

- **Workflows** (`/`): installed workflows, schedule, next execution, latest
  recent run, and installation or query problems.
- **Workflow** (`/workflows/<name>`): schedule and up to 20 recent runs.
- **Run** (`/workflows/<name>/runs/<full-invocation-id>`): task results, errors,
  systemd result when available, and chronological events and output.

All timestamps are UTC. Navigation uses ordinary links; use **Refresh** or your
browser's reload button to update a page. There is no automatic refresh or
live log streaming. Pages and inventory are cached in memory for up to three
seconds; the page cache retains at most 16 pages. Nothing is cached on disk.

Inventory and workflow pages discover runs with journal activity in the last
seven days. "No recent runs" does not mean a workflow has never run. A saved
full run URL can still show older history if journald retains it. Run and
workflow URLs also work after uninstall, subject to available history; an
uninstalled workflow with no recent history has no workflow page.

Each selected run reads at most its newest 1,000 journal records, plus one
record to detect truncation. **Partial history** is explicitly marked: older
tasks, task attribution, and start times may be unavailable. Missing start
times have no duration. A truncated run with no completion evidence is not
assumed successful. Log messages longer than 2,000 characters are shortened
for display. These are record and display limits, not a byte limit on journal
entries or a fixed wall-clock query deadline. Discovery streams the matching
seven-day window, so a busy journal can still take time to query.

Use `floe logs <name> <id>` or `journalctl` for unabridged logs. The CLI's default
history and log commands retain their existing unbounded-per-run behavior.

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
