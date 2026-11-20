# Local reconciliation fixture

```sh
uv run pytest -q tests/agentic_dev_loop/test_tick.py
```

This runs the tick with normalized issue fixtures, a temporary launcher ledger,
and a fixture worker. The first test runs an ordinary Floe `Workflow` and checks
that it returns while the worker is still running. No timer is installed and no
GitHub, model, or user service-manager calls are made.

`Tick.workflow(scopes, report)` builds the run-only workflow for `floe.run()`.
Supply one ordered batch of trusted feature scopes. The tick considers only the
first `batch_size` entries, snapshotted at workflow construction. Every run
rereads evidence for those same scopes; build a new workflow to select another
batch. The report contains one result per considered scope, including deferrals. A
successful Floe run means the tick delivered its report, not that any worker
started or finished. A mirrored note records reservation IDs, not liveness.

Use the same monotonic clock for the tick and its fixture endpoints. Each call
receives an absolute deadline no later than the whole-tick deadline. Adapters
must actually enforce their deadline: the synchronous tick does not interrupt a
blocking adapter. A late read or admission reply does not authorize another
call for that feature in the same tick; known side effects are still reported.
Rate limits and transport outages defer only dependent calls. Adapters must
classify expected failures; programming errors are not treated as outages. Keep the
fixture host's `Cooldowns` across ticks and share bucket IDs only for operations
that rely on the same limited endpoint. This cooldown state does not survive
host restarts; it is not an admission or issue-state store.

Recovery uses a fresh observation and the existing launcher's stable attempt
key. The fixture uses attempt 1 only. A changed input snapshot is not a request
to retry under another key. The logical fixture writer checks for a note again
before insert; any existing note is left alone. There is deliberately no GitHub
mutation adapter, claim of atomic GitHub comment updates, or deployed worker
lifecycle guarantee.

## Design PR refusal fixture

```sh
uv run pytest -q tests/agentic_dev_loop/test_merge.py
```

`attempt_story_squash()` is the fixture's trusted merge entry point, separate
from the tick. Supply the canonical ADR path and any known design issue
identities (including legacy #30 for feature #29) from trusted configuration,
not from the PR. A `Design:` title, an actual closing link to a design issue,
or a change to the canonical ADR blocks squash. This includes deletions and
both sides of renames. The decision log is an ordinary file (D-23); updating it
does not make a story PR a design PR. Paths are exact, case-sensitive repository
paths in the Git diff, not filenames resolved in a checkout. `Design:` is the
same literal title prefix used for feature discovery. Neither the author's
identity nor a request for an exception changes this policy.

The fixture checks every page of both evidence sources before accepting a PR as
non-design. The transport must attest to candidate identity, complete counts,
and exhaustion, or fail the read. The broker reclassifies after the separate
story-gate check; fresh unknown evidence or a changed classification snapshot
blocks squash. Tests stub the story gates and record only the fake squash
request, not a merge result. There is no live adapter, no GitHub enforcement,
and no proof that mutable PR metadata can be checked atomically with a real
squash request. The fixture imposes no restriction on the maintainer acting
outside the loop.

## Live E2E lease

Use one reviewed copy of `live_e2e.py` pinned outside Builder worktrees. Run it
as the authorized test user, with that user's local systemd manager running and
with `systemctl`, `busctl`, and the ordinary Floe test tools available. The
candidate checkout must already be synced with `uv sync`. Manual and automated
callers use the same command (pass the candidate's absolute checkout and
virtualenv interpreter paths):

```sh
python3.14 -I /path/to/trusted/live_e2e.py run \
  --checkout "$PWD" --python "$PWD/.venv/bin/python"
```

The scope is one local Linux host and user-manager UID with the default XDG
configuration and timer-state layout. No remote manager, alternate runtime
namespace, or custom manager layout is supported. Caller `HOME`, XDG and bus
aliases do not select a different lock or target. Keep the private
`~/.local/state/floe-live-e2e/` directory on a persistent local filesystem. Do
not replace the lock file or remove the dirty record by hand. A contender is
refused immediately, before any manager queries. Raw `pytest --systemd-e2e`
requires the inherited lease, even for an individual test module. `-k` on the
launcher selects a subset; record it as such, not as a full-suite result.

The launcher holds the lease through the read-only initial sweep, tests, fixture
teardown and postflight. Prefix matches are evidence of residue, not ownership.
The launcher never stops, unmasks or deletes units; it refuses to start while
residue is present. Fixture teardown removes only fixtures from that run.

If interrupted, or if queries, tests or cleanup failed, inspect the prior
process group and named artifacts. Resolve only artifacts you can establish
were created by the run, using the appropriate authorized cleanup (for example,
`floe uninstall --name <workflow>` for an owned Floe deployment). Leave unknown
ownership untouched. Then use the same trusted script with `recover` instead of
`run` and its arguments. Recovery takes the same lock, verifies that the recorded
process group is gone and that no E2E unit, queued job, unit file, enable link or
timer stamp is visible, and clears the block only on success. A failed query is
not a clean result. A busy lease is not a request to cancel another run.

The lease serializes cooperating callers; it does not isolate code and is not
protection against arbitrary same-user commands, candidate code or old
checkouts that bypass the entry point. Use only authorized fixtures in the
maintainer's session; production automation still needs a protected deployment
and the isolation required by the ADR.
