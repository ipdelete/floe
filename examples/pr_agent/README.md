# Daily pr-agent maintenance

Update `~/src/pr-agent` from upstream every day at **7 p.m. Eastern local time**
(`America/New_York`, including daylight saving time). Verify the update with
`pr-agent doctor`, then push it to your fork. A failed task stops the run and is
visible in Floe's dashboard and journal-backed history.

## Requirements

- The pr-agent checkout at `~/src/pr-agent`, with `main` checked out.
- `upstream` pointing to the upstream repository, `origin` to your fork.
- Git at `/usr/bin/git`, uv at `~/.local/bin/uv`, gh at `/usr/bin/gh`, and
  the configured doctor wrapper at `~/.local/bin/pr-agent`.
- Unattended Git fetch/push authentication, a working `gh auth token`, and the
  wrapper's model credentials (currently `~/.config/pr-agent/key`).
- Network access to GitHub, package sources, and the wrapper's model gateway.

The wrapper must run doctor against the same checkout. Edit the path constants
in `maintenance.py` if your setup differs. The workflow sets a service-safe
`PATH` for the wrapper's uv/gh calls and disables interactive Git/GitHub prompts;
it does not read a shell profile or copy credentials into this repository.
Doctor makes a real model call, even when upstream has no new commits.

## Install

From the Floe checkout, using its durable Python environment:

```bash
uv sync
source .venv/bin/activate
floe install --dry-run examples/pr_agent/maintenance.py
floe install examples/pr_agent/maintenance.py
floe start pr-agent-maintenance
floe list
```

The install enables the daily timer. `floe start` runs it immediately under
systemd; it **updates the checkout and pushes to the fork** on success. Only
service runs appear in the dashboard/history. Keep the Floe checkout and its
`.venv` in place. See [deployment](../../docs/deployment.md) for user lingering,
timer catch-up, and service environments.

```bash
floe runs pr-agent-maintenance
floe logs pr-agent-maintenance
floe uninstall pr-agent-maintenance
```

## Steps and retries

| Task | Behavior | Attempts |
|---|---|---|
| `preflight` | Check tools, clean working tree, and checked-out `main` | 1 |
| `fetch` | Fetch upstream and fork | Up to 2 for network failures |
| `inspect` | Require fork ≤ local ≤ upstream in commit ancestry; log revisions | 1 |
| `fast-forward` | Merge the inspected upstream revision with `--ff-only` | 1 |
| `sync` | `uv sync` in the checkout | 1 |
| `doctor` | Run the configured `pr-agent doctor` wrapper | Up to 2 for failure or timeout |
| `push` | Check clean tree/revision, then ordinary push to origin | Up to 2 for network failures |
| `verify` | Check clean tree and fork's live revision against the target | 1 |

Retries wait **60 seconds**, with two attempts total, not two extra retries.
Fetch/push retry recognized connection/DNS/HTTP 502–504 failures or timeouts;
authentication and rejected pushes fail immediately. Doctor retries any nonzero
exit or timeout, so a failing run can make a second paid model call. Successful
tasks are not rerun during a retry. Subprocess timeouts are two minutes for Git,
ten minutes for sync, and three minutes per doctor attempt. Each command runs
in its own process group; a timeout kills the group (including helpers) and
reaps the command before a retry begins. The installed service's default
30-minute overall timeout includes retries.

## Failure and recovery

There is no stash, branch switching, conflict resolution, force-push, or automatic
rollback. Dirty trees, local-only commits, a fork ahead of the checkout, and
divergent histories require your attention. Do not edit the checkout or run a
second updater while this workflow is running.

If sync or doctor fails, local `main` remains at the upstream revision and the
fork is untouched. The next run accepts that state, repeats sync and doctor,
and publishes only if they succeed. A push that succeeded before a connection
failure is safe to repeat. A final verification failure may occur after the
push has already succeeded; inspect the logs rather than assuming rollback.

The update is pinned to the fetched upstream revision. Upstream changes after
that fetch wait for the next run. This trusts upstream code: dependency syncing
and doctor execute it under your user account, not in a sandbox.
