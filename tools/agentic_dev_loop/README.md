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

## Installed fixture lifetime gate

`tests/agentic_dev_loop/test_installed_fixture.py` builds the candidate Floe
wheel offline and uses a separate, non-editable virtualenv with locked runtime
dependencies. The offline checks verify the console entry point, dry-run unit
files and interpreter path with a deliberately broken mock Builder checkout.
The workflow and loop snapshot are staged read-only outside that checkout;
mutable fixture inputs/observations and the admission ledger are separate.

The two live cases run via the [trusted lease](#live-e2e-lease), as part of the
full suite. They require a local user manager with cgroup v2, journald,
`systemd-run`, and `uv` (on PATH or in `~/.local/bin`, with the locked artifacts
already cached). They verify a timer and two manual requests joining one tick,
and a timeout killing a tick-local child but not the independent worker. A
fresh tick reuses the same reservation; cancellation collects the worker unit
and retains the terminal record. The transient adapter maps the launcher's
reservation to `floe-e2e-worker-<run-id>.service` and checks transient-unit and
run/session ownership before stopping it. Unexpected exits or interruptions
before a verified stop receipt remain ambiguous; a missing unit is not a
terminal outcome. The postflight still checks for residues under the same lease.

No production timer or GitHub mutation transport is configured. A read-only
snapshot and distinct cgroups establish path and lifetime separation for this
fixture, not filesystem or credential isolation from same-user code.

## Supervised persistent Pi smoke

This is opt-in and budgets one Pi prompt. Use the same reviewed lease entry
point below, with the exact selection `-k installed_pi_smoke`. The ordinary
live suite runs credential-free service isolation controls, not the model call.

Before the run, an operator must provision the following under their private
`~/.local/state/floe-pi-smoke-inputs/` (directory `0700`, regular files `0600`,
no links). The fixture does not create tokens, fall back to the operator's
ambient logins, or copy a whole home or Pi directory:

| File | Required contents |
| --- | --- |
| `config.json` | `tick_login`, `worker_login` (exact expected GitHub logins, worker must be the configured maintainer), `node`, `release` (absolute paths to the reviewed Node executable and pinned Pi release directory) |
| `tick-token` | Already-provisioned fine-grained GitHub credential scoped to `ipdelete/floe`, with read-only permissions including Issues read |
| `worker-token` | Separate credential for the maintainer's `gh` login |
| `copilot.json` | Pi-native auth JSON containing only the `github-copilot` entry |
| `models-store.json` | Reviewed Pi catalog cache containing the pinned Copilot model, without secrets or extra provider configuration |

Use a trusted Pi release supporting the print and session flags (validated CLI
contract: Pi 1.5.2). The host needs `/usr/bin/python3.14`, `gh`, `git`,
`libkeyutils.so.1`, Bubblewrap with unprivileged user namespaces, cgroup v2,
and effective systemd socket and syscall restrictions. The two transient services start without a
terminal or shell activation. Each mounts only its explicit role inputs;
neither gets the host home, runtime directory, inherited environment or access
to the Unix sockets or kernel keyring. The live probes check these conditions
before touching auth. The tick makes fixed GETs, including a bad-auth control;
checking these reads does not establish the permissions of a misprovisioned
GitHub token. The operator must verify its read-only scope at provisioning.

The trusted supervisor runs the read-only tick probe first, then uses the
fixture reconciliation and launcher to admit one worker. This deliberately
keeps the manager bus out of both credential-bearing processes. This is not a
new installed timer or a production GitHub tick adapter. The ordinary lifetime
fixture above separately checks Floe's installed timer and tick/worker overlap.
The worker checks the exact login, then runs Pi in a disposable, read-only Git
worktree with one fixed prompt, no tools or extensions, and retries disabled.
The probe checks the response and native session header, name, and filename;
only after collection and rechecking the file can the launcher archive success.
The worktree is removed. The launcher ledger and any native session remain
private; publish only a safe result, never their paths or contents. Missing
provisioning, ineffective isolation or unconfirmed status blocks the smoke;
passing it does not authorize unattended work or establish isolation for
arbitrary role code, other local services, or a hostile same-UID operator.

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
