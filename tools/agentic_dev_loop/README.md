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

## Private closeout staging

The fixture supervisor calls the Python staging entry point
`closeout.stage_closeout(root, policy, evidence, draft, sessions)`. It accepts
normalized fixture evidence, not GitHub or Pi commands. Keep `root` an existing
owned `0700` directory outside the checkout, under operator-controlled parents.

1. Admit the Archivist only once readiness offers it. It writes the summary for
   closeout separately from its native session. Keep the `CloseoutDraft`
   (feature, configured slug, approved squash, Archivist session ID and summary)
   private. The summary must be nonempty UTF-8 prose of at most 64 KiB; the
   stager cannot establish whether prose contains sensitive or quoted material.
2. After the Archivist exits, give the stager a fresh, complete readiness
   snapshot and the complete session roster. Each `Session` must be bound by
   the supervisor to its actual role, issue, attempt, canonical ID, absolute
   local native path and verified process completion. A file or reservation ID
   is not completion evidence. The stager checks the approved backlog again
   independently of whether a second Archivist could be admitted.
3. The stager checks the bounded native Pi header for identity, not body records
   or dialogue. An absent native file can yield the fixed
   `native-session-missing` omission only if explicitly permitted for that
   finished session. Unreadable or invalid native headers block; the draft's
   Archivist must be present, verified, finished and never omitted.
4. On success, hand off the returned `FrozenCloseout` without another model
   session. It carries the exact immutable `index.json` and `summary.md` bytes
   and the location of their private read-only copies. The version-1 index
   contains generated feature/session labels, issue/PR/squash links, verified
   session IDs and permitted omissions. It contains no session paths or native
   body records. Each successful call creates a new directory; it cannot edit a
   prior candidate. Treat only a successful handoff as a candidate, not stray
   directories left by an interrupted local write.

Staging does not scan or publish. Filesystem modes aren't isolation from the
owner, and snapshots aren't locks against remote changes. Do not upload the
staged tree. Run the staging tests with
`uv run pytest -q tests/agentic_dev_loop/test_closeout.py`.

### Local pre-push policy

The trusted publisher calls `closeout_policy.release_closeout()`, not the
Archivist or candidate checkout. Configure the absolute private staging root (with trusted parent
directories), expected issue and slug, and absolute path to the standalone
Gitleaks **v8.30.0** Linux arm64 or x64 release binary. Install it separately
from the [upstream release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.0);
the gate pins the extracted executable's SHA-256 and runs a private copy of
exactly those verified bytes. Execution from the private scratch directory
under `/tmp` must be permitted; a `noexec` mount produces a scanner error.
The gate never searches `PATH`, downloads a scanner, or loads candidate rules.
Deploy it from a protected snapshot, not from files workers can modify.

The gate checks that the private handoff contains only a version-1 `index.json`
(up to 256 KiB) and a nonempty UTF-8 `summary.md` (up to 64 KiB), with no
symlinks or transcript paths. It holds on residual credential shapes and
common private path patterns rather than rewriting input. Gitleaks uses its
built-in rules plus a required seeded Bearer control, without inline allow
comments. It scans the exact index and summary bytes and the index's decoded
string values. The summary is scanned as plain UTF-8, not rendered Markdown.
A clean scan is **not** evidence that prose is private-data-free or independently
authored. Never submit dialogue, session exports or tool output as a summary.

On a hold the callback isn't called, the stage stays private, and the result
is a fixed `ReleaseStatus`, never a scanner report or matching text. Correct
problems privately and rerun the whole gate; there is no waiver. After a clean
scan the gate rechecks the staged copies and passes only the two immutable
path/byte pairs under `.agents/engineering/<feature-id>/` to `publish`. The
callback must write exactly that delta without consulting staging again.
Callback exceptions propagate because once the writer has been invoked a write
may already have happened. Do not retry on that basis without reconciliation.
The filesystem checks are not a lock against another process; never run
untrusted code in the publisher's security context.

Run the policy tests offline with
`uv run pytest -q tests/agentic_dev_loop/test_closeout_policy.py`. Set
`FLOE_TEST_GITLEAKS=/absolute/path/to/gitleaks` to include the real pinned
scanner's clean, Bearer and built-in rule controls on synthetic inputs. The
normal suite otherwise tests the subprocess boundary with fakes.

### Sealed publication and closeout squash

`closeout_release.Publisher` is a trusted supervisor entry point, not a worker
command or a general GitHub writer. Give it the private `FrozenCloseout` but
no native session store. Hold its permit, journal, scanner configuration and
transport outside the worker. The permit is an in-process capability, not a
new human approval or a security boundary against arbitrary code in that
process. Do not expose the generic writer, transport, or a mechanism for
supplying callbacks to the Archivist.

The publisher checks the scanned index against fresh approval and completed
backlog evidence. Its adapter prepares a commit with the pinned `master` base
as sole parent, fixed trusted metadata, and just the two sealed regular-file
additions. It may not read a checkout or reopen staging. The only allowed
push is creation of `feature/<feature-id>-closeout`; the PR targets `master`
and its generated body closes just the feature. Both writes use durable claims
under the shared writer lock. After an interrupted reply, reopen the same
journal and supply the same handoff and target. The publisher must observe the
exact ref or expected PR before advancing. An empty or conflicting read does
not authorize another send, even after a timeout. If the result stays
unresolved, surface the status to the maintainer and preserve the journal. This
fixture does not overwrite existing closeout files, rebase or replace a
published candidate, or repair unexpected remote state.

Run the independent check **from the separately protected tooling root**, not
from the PR, its worktree, or a candidate-selected policy or command:

```sh
python3.14 -E -s -B -m tools.agentic_dev_loop.hosted_closeout \
  --repo /path/to/protected/git-objects --repository OWNER/REPO \
  --feature FEATURE_NUMBER --slug FEATURE_SLUG --base MASTER_SHA --head PR_HEAD_SHA \
  --scanner /path/to/pinned/gitleaks
```

Provision `/usr/bin/git` and a protected Git SHA-1 object store with valid,
complete objects for these trusted event identities, fixed local config,
normal Git intake validation and resource limits. The checker inspects only Git objects, not a
checkout. It checks the complete diff from the sole parent, requires exactly
the two non-executable regular-file additions, and runs its own pinned policy
on those bytes. It exits 1 with a safe status on refusal; success emits only
the base SHA, head SHA and SHA-256 digest of the paths and blob bytes. Local
stdout is **not** authenticated hosted evidence. A hosted adapter has to bind
that result to the exact PR and candidate and authenticate the configured
protected workflow identity/revision independently of its displayed name.

`Publisher.merge()` requires the verified publication receipt, current
same-repository open PR and feature, exact hosted success, current `master`,
and the same completed backlog with no redesign hold. The conditional squash
transport must enforce those facts at the write boundary, not just do a client
reread before a head-only merge. The broker journals the claim and verifies the
actual squash SHA, sole parent, delta, `Refs: #<feature>` trailer and feature
closure attributed to that exact merge of the PR. It never directly closes the issue. It can
reconcile a delayed closure or lost merge reply by PR and issue number without
the topic ref. Unexpected merge or closure and unresolved sends remain
blocked; do not clear a claim on that basis.

```sh
FLOE_TEST_GITLEAKS=/absolute/path/to/gitleaks uv run pytest -q \
  tests/agentic_dev_loop/{test_closeout_release,test_hosted_closeout}.py
```

This is fixture wiring only: no deployed GitHub write adapter or hosted trigger
is installed. The pilot still needs to demonstrate trusted hosted triggering
and identity, the real write conditions, and GitHub auto-close from the
expected squash. Do not enable public closeout from these local results alone.

## Checked story squash fixture

```sh
uv run pytest -q tests/agentic_dev_loop/{test_merge,test_story_merge}.py
```

`story_merge.MergeBroker` is the fixture's story squash entry point, separate
from the tick and the closeout publisher. Configure the canonical ADR path and known design issue IDs
(including legacy #30 for feature #29) in trusted policy. A `Design:` title,
actual closing link to a design issue, or change to the canonical ADR (including
deletions and both sides of renames) blocks squash, regardless of author. Diff
paths are exact and case-sensitive. The decision log is not the ADR (D-23).
Classification requires complete, candidate-bound pages with verified counts
and exhaustion, not a list possibly cut short at an endpoint cap.

The broker checks current readiness for the exact story and PR, the approved
ADR revision, the `master` base and head, nondraft status, clear reviews and
complete PR-Agent findings disposition. CI, PR-Agent and E2E must come from
separately configured trusted producers for the same base/head/tested merge SHA
and revision. E2E must attest to the same CI and PR-Agent runs and verified
cleanup; the supervised desktop fixture receipt is not accepted. The broker
reads the gates and classification again before claiming the write. A
conditional transport must enforce the expected facts *at* the squash, not
just reread them before sending a request. In particular, a head-only lease
does not guard base movement. No live transport is provided; unattended GitHub
merge stays disabled. Tests do not prove GitHub's base/metadata conditions,
branch protection or hosted auto-close.

Use the protected `initialize_writer()` journal and the same writer lock for
cooperating callers. A loop redesign must first call `register_redesign()`;
`REGISTERED` means a durable merge hold exists for that feature and revision,
not that the remote request was published. Do not publish a stale request for
an obsolete revision. A competing call gets `BUSY`, and an unresolved squash
blocks the feature's redesign registration. External actors aren't locked out.
Old fixture writer journals without the merge tables cannot authorize merges;
do not delete or recreate a journal with pending operations to unblock them.

The broker claims the squash before sending once with explicit `Refs:` trailers
for the feature and story. It reconciles by stable PR and issue number, even
if the topic branch is deleted. Completion requires an independently read
squash SHA distinct from the tested SHA, the expected base parent and message,
and an issue closure attributed to that PR. The comparison allows trailing
newlines, not a provider-added suffix. Hosted message preservation is unproven.
Premature closure, a wrong commit or missing auto-close is a discrepancy.
Reconcile again if evidence is delayed; a persistent discrepancy requires
operator investigation of the PR, commit and issue timeline. It does not
clear a squash claim or unblock redesign publication, and there is no force
or reset path. An unresolved reply never authorizes another send, even if the
PR still looks open. Only a definite rejection at the write boundary clears
the claim for fresh validation. Keep the same request and journal on recovery;
a verified receipt records the historical effect, not current issue state.

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
