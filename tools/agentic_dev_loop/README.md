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

## Reconcile a GitHub write

`github_writes.Writer` is the fixture entry point for pushes, issue creation
and closure, sub-issue links, PR creation, and reservation/terminal writeback.
It does not replace the tick's fixture port, perform role authorization, or
merge anything. Create an issue with a stable marker, recover its issue ID,
and link it to the parent as a separate operation. A link does not reparent a
child. Run notes append to the issue's attempt history; no comment is edited
and no GitHub comment compare-and-swap is assumed.

Provision once with `initialize_writer()` in a protected local directory. All
cooperating callers for the repository must share that directory and the same
lock inode, including after restarts. Supply a stable operation ID from trusted
inputs, not a new one on each call. The journal binds the ID to the complete
request, commits a send claim before transport I/O, and keeps verified
receipts. A repeated verified call returns the stored receipt, not a guarantee
that the remote object is still unchanged. One issue marker or PR branch pair
identifies one creation target; a stale empty search cannot erase a verified
creation. Provision under an operator-controlled parent and keep the directory
private; the lock does not isolate same-UID code. `BUSY` means no I/O, not
queued work. Both transport calls must honor the supplied absolute monotonic
deadline. An unexpected adapter exception propagates, leaving any committed
send claim for the next reconciliation.

A successful transport reply still requires a read of the exact result. If a
send might have landed, continue reconciling with the same ID. An unchanged
ref, empty search, timeout or lost receipt is not enough to replay. Only a
push can replay, and only with positive proof that the exact send generation
cannot still apply, unchanged original ref conditions, and an exact old-ref
lease at send. No live adapter supplies this proof. An unresolved send blocks
other IDs targeting the same object; changing or deleting the journal to get
past the block is unsafe. Terminal notes are fingerprinted durably so stale or
conflicting evidence cannot authorize a regression. If inspection stays
inconclusive, hold the affected work for operator investigation; there is no
automatic rollback or forced retry.

```sh
uv run pytest -q tests/agentic_dev_loop/test_github_writes.py
```

These tests use normalized fixture evidence only. They do not prove live
search completeness, endpoint race behavior or GitHub mutation semantics.

## Role handoffs

```sh
uv run pytest -q tests/agentic_dev_loop/test_handoffs.py
```

Pass issue artifacts to the next role, not the previous role's conversation.
`handoffs.role_input()` binds a readiness offer to the feature, actual target
issue, role, attempt and exact approved design where applicable. Use the same
configured feature slug throughout. Attempt 1 uses the unsuffixed names; a
retry adds its attempt number to the branch, session and private worktree name.
Worktree allocation, ownership, GitHub writes and actual admission remain
separate operations.

| Role | Read | Hand off |
| --- | --- | --- |
| Intake | Maintainer's requirements and decision seed | Put both on the feature issue (`enhancement`). Link actual spike and design sub-issues and record the named Intake session and requirements comment with `intake_note()`. |
| Spike | One linked question and its designated design issue | Link the attempt's branch in Development. Verify the pinned commit is reachable from that retained branch, then record `spike_delivery()` on the design issue before closing the spike. No merge PR. |
| Designer | Intake requirements, spike pins and the previous approval for a revision | Open the design PR against `master` to close only its linked design issue. Leave approval to the maintainer; a revision uses a new design issue and PR. |
| Planner | The canonical ADR at the verified maintainer squash SHA, not at current `master` or in a worktree | Inventory story markers and actual child edges. Reuse an exact matching issue, verify acceptance checks and prerequisite links, and publish the backlog only after rereading the current revision bindings and explicit dispositions. |
| Builder | One linked story, exact approval, and one private worktree and branch per admitted attempt | Test first, then link the story PR to the attempt and include exact-candidate evidence. On a design contradiction, record `revision_request()` on the story rather than editing the ADR in a story PR. |
| Archivist | The current backlog and retained issue, session, PR and commit links | Reconcile an index of safe IDs and prepare closeout privately. Publication is a separate gate; no transcript is a handoff. |

For phase sessions, `issue_note()` takes a reservation from the launcher and an
expected attempt key bound to the issue's stable GitHub IDs by the caller. It
renders only public identities and canonical session/run UUIDs. A matching
Development link or a note cannot reserve a slot or attest to liveness. Do not
include local paths, private session locators, raw logs or tool output; any
requirements, acceptance checks or explanations are purpose-written prose.

Use a feature-scoped marker for each `StorySpec.key`, stable across revisions.
`reconcile_plan()` requires complete marker reads (including unlinked matches)
and a fresh verified approval. An unlinked or conflicting match blocks instead
of requesting another issue; recover prior writes through the writer. New issue
and rebind suggestions are not writes. A previous backlog must be reconciled,
including explicit disposition of any unlinked story. Fixture adapters must
supply `FeatureEvidence.revision_requests` explicitly: a complete trusted
history, `()` for verified absence, or `None` for an unknown read. A request
against the current approval stops Builder offers and story merge eligibility.
Link a new design child to route the request to Designer. A subsequent verified
approval still needs a reconciled backlog before either can resume.

Use `pr_body()` for the child-only `Closes` and nonclosing feature `Refs` header.
Verify actual closing links, the recorded same-repo head name and the pinned
story approval or design predecessor with `pr_trace()`, not a branch URL. Use `valid_work_commit()` on work commits and
again on the squash message. Keep the issue → PR → squash link and the spike
commit pin independently of topic branch links. Fixtures exercise these routes
with local Git refs; hosted closure and deployment are not tested here.

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

Before the run, the maintainer provisions the probe from their existing `gh`
and Pi logins. With the reviewed Node executable on `PATH` and the managed Pi
1.5.2 release installed, run from the checkout:

```sh
uv run python -m tools.agentic_dev_loop.pi_smoke provision
```

This creates `~/.local/state/floe-pi-smoke-inputs/` (`0700`, regular `0600`
files, no links). It stages one existing GitHub token for both services and
records the exact login that token identifies, the absolute Node and Pi paths,
and only the Copilot auth and pinned model catalog entries. It does not
create tokens, print credentials, copy a whole home or Pi directory, or
replace existing provisioning. For a refresh, with no smoke running, move
an existing input directory to another private location before provisioning
again. That directory can also contain earlier private sessions and ledgers.

The host needs `/usr/bin/python3.14`, `gh`, `git`,
`libkeyutils.so.1`, Bubblewrap with unprivileged user namespaces, cgroup v2,
and effective systemd socket and syscall restrictions. The two transient services start without a
terminal or shell activation. Each mounts only its explicit role inputs;
neither gets the host home, runtime directory, inherited environment or access
to the Unix sockets or kernel keyring. The live probes check these conditions
before touching auth. Both services use the maintainer's existing `gh` token
(D-11). It is **not** a read-only credential: the trusted tick code makes
only fixed GETs, including a bad-auth control. This is a code restriction,
not a GitHub permission boundary. The tick receives no Copilot auth or model
catalog.

The trusted supervisor runs the read-only tick probe first, then uses the
fixture reconciliation and launcher to admit one worker. This deliberately
keeps the manager bus out of both credential-bearing processes. This is not a
new installed timer or a production GitHub tick adapter. The ordinary lifetime
fixture above separately checks Floe's installed timer and tick/worker overlap.
The worker checks the exact login, then runs Pi in a disposable, read-only Git
worktree with one fixed prompt, no tools or extensions, and retries disabled.
The probe discards Pi's stdout. Raw stderr stays in sandbox tmpfs; only an
exit code and fixed diagnostic categories are saved privately. It checks the
native session header, name, filename, and pinned model reply. Node's compile
cache is disabled so its shutdown writes do not hit the sandbox's file size
limit. Only after collection and rechecking the session can the launcher
archive success.
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

## Checked E2E fixture attestation

`checked_e2e.validate_fixture()` runs the full live suite through the same lease.
A trusted supervisor supplies the fixture pin, interpreter, manager context and
receipt path. Its reader supplies **current** evidence for the exact
(repo/PR, head SHA, base SHA, tested merge SHA, ADR revision). CI and PR-Agent
must each report success from the configured producer, with a complete review
whose findings are all fixed or explained. Producer identities are facts from
an authenticated reader, not visible check names, PR comments or candidate
files. Missing evidence, a draft/closed candidate or unknown/pending redesign
blocks the run. The supervisor checks evidence again at launch and after cleanup;
these observations are not an atomic check against external changes.

Stage the reviewed SHA-1 Git commit as a plain tree (no `.git`, `.venv` or
extra files or directories) from a protected Git source. Provision a separate trusted Python environment
with the fixture's dependencies and a non-editable install of the pinned Floe
code. The gate checks file bytes and executable bits against the Git tree and
holds the directory descriptor used as the child's working directory. Symlinks,
submodules, and trees with archive-time exports that differ from Git are not
supported. Tree checks do not protect against a writer who can mutate the
staging area in place during the run, or tamper with the interpreter or tooling.
Those paths must be controlled by the supervisor.

For one supervised demonstration, after confirming no legacy run and no
`floe-e2e-*` units, run **from the separately protected tooling root** (not the
fixture or a Builder worktree). The desktop command accepts only the reviewed
master snapshot selected for #48:

```sh
python3.14 -E -s -B -m tools.agentic_dev_loop.checked_e2e fixture \
  --repo /path/to/protected/git-repo --pin a2a3c9deb3a8225178382ce5c7c1e649b952daa5 \
  --checkout /path/to/staged-tree --python /path/to/fixture-env/bin/python \
  --receipt /path/to/private-output/new-receipt.json
```

This command uses synthetic CI, review, PR, head, base and ADR identities. It
makes no GitHub calls. The supervisor writes a new private
`supervised-fixture-e2e` receipt under the lease only after a zero test exit,
verified cleanup and final evidence and tree checks. It never replaces a prior
receipt. A fixture receipt is not a story merge credential. The desktop entry
has no unattended candidate mode or authenticated GitHub evidence adapter.
Do not enable candidate E2E until an approved isolated boundary keeps candidate
code away from the trusted supervisor, unrelated resources and manager sockets,
and personal/production credentials.
