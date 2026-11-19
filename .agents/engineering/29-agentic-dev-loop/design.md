# ADR: Agentic development loop

**Status: Proposed — awaiting maintainer approval via the design PR**

## What approving this PR means

Approval accepts the architecture and resolves each [decision requested below](#decisions-requested-from-the-maintainer). Accept the recommended answers or record your alternatives in the PR before merging. Approval does **not** authorize creating the GitHub App or throwaway repo, installing units, changing repository settings, or enabling unattended merges.

**Nothing runs unattended until both the GitHub identity and approval gate and the deployed tick and worker pass validation.** Until then, setup and validation are supervised and separately authorized. Story merges and public closeout also have their own validation gates.

[Feature #29][feature] · [Design #30][design] · [Decision log](decisions.md)

## Recommendation

Use a short Floe timer run (a **tick**) to **reconcile** work: check what GitHub says actually happened before deciding what to do next. Run one persistent Pi CLI worker at a time in its own systemd unit. Keep the approved design on protected `master` and use a separate GitHub App identity. Prove the App can merge a checked story but cannot land an unapproved design. Run live end-to-end tests (E2E) one at a time in their dedicated systemd environment. At public closeout, publish an index and a purpose-written summary, not session dialogue.

GitHub keeps state where the maintainer already works. The tick exits instead of paying for a model session to wait. Both runners fixed the same fixture [#31][s31]. Pi-durable can resume from a checkpoint, but it cannot tell whether an interrupted push landed [#32][s32]. Start with the simpler worker; either runner still needs to check external results and control what it publishes.

## Context and scope

A sentence becomes a feature issue. The maintainer clarifies requirements and approves the design. Agents turn the approved backlog into tested, merged changes. The feature issue links the evidence, decisions and sessions. Use one lifecycle, not selectable modes. A design revision goes through the same gate (D-06). Bootstrap and validation limit rollout; they do not create a second mode.

Use Floe as an ordinary installed workflow. Out of scope: changes to Floe's runtime dependencies or public API, a new project-management database, a new model tool suite, multi-host failover, and general scripting of deterministic work (D-08). The controls below protect worker starts, GitHub writes and publication. They do not form a second feature-state store or a general workflow framework.

Normal repo safety rules still apply. Document D-05's standing scope for story E2E in `AGENTS.md` before unattended use. It does not authorize installing the orchestrator or experimenting in the user's systemd session.

## Components

The first slice is local: test one tick against GitHub and launcher stand-ins, then test the launcher with a fixture worker. **That slice needs no App, live timer, production credentials or merge service.** The table shows when each real component is needed.

| Component | What it does | What it holds | When needed |
| --- | --- | --- | --- |
| Floe tick | On each timer run, checks GitHub and requests starts for ready work; exits without waiting. | Pinned Floe wheel and protected config; scoped read access and permission to request starts, no model credentials. | First slice as a local command; deployed later. |
| GitHub issues and branch rules | Hold feature progress and evidence; enforce review and check requirements on `master`. | Issues, PRs, commits and protected design and policy files; maintainer owns settings and design approval. | Stand-in first; real rules before unattended operation. |
| Launcher | Handles **admission**: decides whether to reserve and start a worker attempt or return the existing run. Owns each worker's lifetime. | A private **ledger**, the durable record of attempt keys, run/unit IDs and outcomes; scoped access to manage its own units. | Fixture in first slice; real units for deployment validation. |
| Pi worker and private session store | Run one role per attempt and keep the native session; do not grant themselves permissions. | Isolated worktree, scoped tool access and noninteractive model access; private session storage and IDs. No App key or personal credentials. | Fixture worker first; real Pi for deployment validation. |
| GitHub App and credential issuer | Give automation a distinct identity. The issuer creates narrowly scoped installation tokens. | Issuer keeps the App key; approved operations use short-lived tokens. No worker gets the key. | Later, in an authorized throwaway repo before production. |
| Merge broker | The trusted code that performs protected GitHub writes and merges only when the required evidence matches. | Scoped App write access, trusted policy and expected PR, revision and commit IDs. | Later, for the identity and merge tests. |
| Trusted CI and review | Run checks and attach CI and PR-Agent evidence to the exact candidate. | Protected check configuration and credentials separate from candidate code. | Later, for App, story and closeout tests. |
| Test manager and host | The **test manager** is a dedicated systemd user instance for live E2E on the automation host or VM. Keep it separate from control services and unrelated units. | Isolated test identity and generated test units; no personal or production credentials. | Later, for authorized E2E validation. |
| E2E lock | Lets only one caller use that test manager, including setup and cleanup. | One lock per manager; block reuse after a crash until cleanup is verified. | Before any automated live story E2E. |
| Trusted publisher | Checks and releases only the exact closeout artifact that passed policy. | Sealed candidate, pinned scanner and scoped publication access; no raw sessions needed to push. | Later, for the closeout slice. |

The launcher, merge broker and trusted publisher can be roles in **one trusted service**, not three daemons. Its credential issuer can share that process; keep the key out of workers and test jobs. The launcher can also enforce the E2E lock. Keep the tick short-lived and run workers outside its systemd process group. Tests use their own identity and manager, and CI checks publication independently. The diagrams show who does what, not separate services for each role. GitHub writes shown from the tick or agents go through the relevant trusted operation.

## Roles and handoffs

| Role | Responsibility and evidence |
| --- | --- |
| Intake | Interactive request and requirements interview. Create the feature index (`enhancement`), record the session, requirements and decision seed, and link spike and design sub-issues. |
| Spike | One question, issue, named session and branch per spike. Push and retain the branch, pin the result on the design issue, then close the spike. No merge PR. |
| Designer | Synthesize evidence into the ADR and decision log. Propose a design PR; revisions get their own design issues and PRs. Cannot approve a design. |
| Planner | Read the exact merged ADR revision. Create or reconcile linked, verifiable story issues with acceptance checks and explicit prerequisites. |
| Builder | One issue, worktree, branch and session per attempt. Red → green → refactor; resolve review findings and supply exact-candidate evidence. Report design contradictions instead of deciding them in a story PR. |
| Archivist | Reconcile the session/artifact index, prepare the closeout artifact privately and submit only policy-approved bytes. Raw capture stays in native harness storage. |

Hand off through artifacts, not shared conversation. A worktree prevents edit collisions; it does not isolate credentials or the host. Use separate OS identities or validated isolation. Keep workers away from the maintainer's home, SSH agent, keyring, Git helpers, manager socket, and CI and issuer credentials.

Give each role only the operations it needs. An allowlist that includes unrestricted `bash` is not an access boundary. GitHub's Contents write permission covers both push and merge, so route protected writes through the broker and closeout through the publisher. Treat issue text, code and sessions as input, not permission to expand scope or privileges.

```mermaid
sequenceDiagram
    actor M as Maintainer
    participant I as Intake
    participant G as GitHub
    participant T as Floe tick
    participant S as Spike
    participant D as Designer
    participant P as Planner
    participant B as Builder
    participant A as Archivist
    M->>I: Request and requirements interview
    I->>G: Feature index, session and child issues
    T->>G: Reconcile ready work and current revision
    T->>S: Admit each ready spike
    S->>G: Pin results and close spike issues
    T->>D: Admit design after evidence is present
    D->>G: Open design PR
    M->>G: Approve design; merge records approved revision
    T->>P: Plan against merged ADR
    P->>G: Link stories with acceptance checks
    loop Until all current stories are complete
        T->>B: Admit next eligible story
        B->>G: PR, validation and review evidence
        alt Approved design still fits
            T->>G: Request gated story merge via broker
        else Design contradiction
            B->>G: Record blocker and revision request
            T->>D: Pause feature story starts and merges; admit revision
            D->>G: Open revision PR
            M->>G: Approve revision; merge records new ADR
            T->>P: Reconcile backlog against new revision
        end
    end
    T->>A: Admit closeout after current backlog is complete
    A->>G: Publish policy-cleared closeout PR
    T->>G: Reconcile gated closeout merge and feature closure
```

## GitHub state and traceability

| Evidence | Rule |
| --- | --- |
| Feature and phase | The feature issue is the index. Paginate actual parent/sub-issue edges, not checklists or cross-references. Use stable `Spike:`, `Design:` and `Story:` titles for discovery (map the already linked #30–#35 by issue ID); use issue numbers for identity. Sub-issue order is priority, not a dependency. |
| Active work | Record role, attempt, run ID and session ID on the issue. Link the branch and PR when known. A branch link does not reserve a worker slot, and an issue note only mirrors the ledger. Unknown reads or conflicting candidates block affected work, not the whole tick. |
| Spike completion | Pinned result commit on the retained spike branch, design-issue handoff and closed spike issue, or an explicit disposition before design. |
| Design approval | Matching design PR to `master` merged through the gate, matching issue, and canonical ADR at that merge on protected history. Pin the approved revision and resolve its path from trusted configuration; a draft branch, shadow file, symlink, review alone or closed issue is not approval. |
| Story completion | Open, in-scope, unblocked stories are eligible only without active work or a pending design revision. Done means the expected PR merged and the issue closed; record its squash SHA. An early closure or missing auto-close is a discrepancy, not success. When revising, explicitly dispose of superseded stories and invalidate old approval-dependent evidence. |
| Closeout | All expected stories under the current approved revision are done or explicitly superseded; no unresolved design revision. The expected closeout PR and squash SHA plus feature issue closure establish completion. |

Use `<feature-number>-<slug>` in branch names, artifact paths and session names. Include the child issue number and attempt where applicable. Name the intake session once the feature has an ID. Link work branches in Development, but keep pinned spike results and PR and squash links that survive branch deletion.

In design and story PR bodies, use `Closes #child` and a nonclosing reference to the feature. Only closeout uses `Closes #feature`. End work commit messages with `Refs:` trailers for the feature and child where applicable. Write those trailers explicitly into the squash message and verify the resulting commit. The [decision log](decisions.md) records phase, decision maker, status, issue/PR, evidence and session. Publish IDs and safe labels, not private file locations, raw logs or tool output.

## Tick, worker and recovery contract

**Tick.** Install a pinned, non-editable Floe wheel. Keep the tick and its versioned config outside Builder worktrees and writable only by trusted operators. Use absolute tool paths, short deadlines and bounded batches. Back off on outages and rate limits. Let unrelated items progress when one fails. A timer triggers a check; it is not a durable job queue.

**Worker lifetime.** The generated one-shot service uses `KillMode=control-group`. The [orchestrator review][review] infers that systemd stops workers left in the tick's control group (cgroup) when the tick exits. No live test confirmed this. Do not start a background or detached child of the tick. **Validating worker lifetime (backlog seed 4) is a hard prerequisite: no real worker is admitted until it passes.** Run each CLI attempt in its own unit, or use a separate pi-durable HTTP host that bounds and cleans up worker subprocesses. Unit names help detect collisions but cannot replace the ledger.

**Starts and recovery.** Use `(repo ID, feature ID, issue ID, role, attempt)` as the attempt key. Record an immutable fingerprint of the request, including the approved revision and input snapshot where relevant. One launcher controls slots and prevents other writers from changing its ledger. In one write, reserve the key, session ID and stable run/unit ID before acknowledging. Reuse the key if a reply or issue update goes missing. Reject different inputs for the same key. Start a new attempt only after checking external state and applying explicit retry policy.

Keep completion records after systemd removes units. Launch a reserved attempt on recovery only if you can prove it never started. A missing unit without a completion record proves nothing. If the launcher loses its state or inspection cannot resolve uncertainty, stop. For pi-durable, also persist the exact storage and conversation mapping. Reuse `requestId` in that conversation and resume pending work at boot.

**Status and cancellation.** Authorized callers can start or fetch an existing run, ask for status by key or run ID, or cancel safely more than once. Confirm cancellation, clean up the owned unit and processes, and check remote effects before freeing the slot or starting a replacement. Cancel cannot undo remote changes. Route tick writeback through one logical writer in the trusted service. Reread uncertain updates, handle conflicts and never revert a terminal status. Do not assume GitHub comments offer atomic conditional updates.

**External changes.** Give each push, issue or sub-issue creation, PR creation or merge a stable operation ID and expected state (such as a ref and SHA, parent and marker, or PR, base and head). Check GitHub before changing anything or retrying. Record the exact result if it already exists. If it is definitely absent, check that the original conditions still hold. If results conflict, a write may still be in flight, or the state is unknown, stop and check again later. Do not auto-retry when the endpoint cannot distinguish absence from a late first write.

A reported failure is not a reason to repeat a write. Keep Durable writes non-replayable unless evidence shows a tool can safely repeat (`safe`). A worker exit, an HTTP success or Durable `done` cannot prove the required external result.

```mermaid
sequenceDiagram
    participant T as Floe tick
    participant G as GitHub
    participant L as Trusted launcher
    participant W as Separate worker unit
    T->>G: Read issues, approved revision, PRs and refs
    T->>L: Start or get by stable attempt key
    L->>L: Persist admission and unit identity
    L->>W: Launch under separate service lifecycle
    alt Reply received
        L-->>T: Existing run ID and status
        T->>G: Record run ID if consistent
    else Reply lost or tick dies before writeback
        Note over L,W: Worker is independent of the tick cgroup
        Note over T: Next tick, same attempt
        T->>G: Re-read issue and external effects
        T->>L: Retry same key if admission still needs resolving
        L-->>T: Same run ID; no second launch
        T->>G: Reconcile missing or already committed writeback
    end
    T->>G: Verify expected external result before advancing
```

## Approval and merge gates

**Before the App.** At the time of [#34][s34], `gh` used the maintainer's login and `master` did not require a design review. Don't give workers the personal login. Agents can prepare local changes; a trusted operator can submit a topic branch and hand off PR creation. For #30, the maintainer reviews and merges the exact candidate under their own identity. Record that decision and the merge SHA. If the sole owner authored the PR, don't claim a GitHub approving review: GitHub blocks self-approval. This manual approval does not prove that GitHub enforces the gate.

**Production after validation and separate authorization.** Install a dedicated App on only the approved repo. Grant only the needed Contents, PR and Issues permissions, plus read access to checks or statuses if needed. Do not let the App change settings or workflows, publish trusted checks, bypass rules, or use personal `gh` as a fallback. The issuer keeps the App key; the broker limits which operations each role can request.

Require PRs and trusted checks on `master`. Require the maintainer's approval on paths listed in `CODEOWNERS`, dismiss stale approvals, and require zero generic approvals. Allow no bypass, including in inherited or classic rules. Make the maintainer the only effective design owner. Protect the authoritative ADR paths (including this `design.md`), `CODEOWNERS`, and the policy and CI inputs themselves. Protect or move check logic out of ordinary story paths.

A PR that touches any protected design or policy path needs the maintainer's approval even if labeled a story. Verify the actual rules, actors and check source, not a label or bot-authored status. The App can press Merge **after** the owner approves, but cannot supply that approval. Path rules cannot tell whether ordinary story code departs from the design.

```mermaid
sequenceDiagram
    participant D as Designer via App
    participant G as Protected master / GitHub
    actor M as Maintainer code owner
    participant T as Tick
    D->>G: Design PR for canonical ADR; Closes design issue
    Note over G: Missing owner approval blocks merge even with green CI
    M->>G: Review and approve current candidate
    opt Candidate changes and approval is invalidated
        D->>G: Update candidate
        M->>G: Approve revised candidate after review
    end
    D->>G: Request gated squash via trusted broker
    G-->>T: Merged PR, squash SHA and linked issue
    T->>G: Read canonical ADR at merge and verify linkage
    Note over T: Only now eligible for planning against that revision
```

**Story gate.** The trusted service records the nondraft PR, its latest commit (head SHA), the target base and tested merge candidate, the ADR revision, test results and PR-Agent review for that head. Builder checks the findings, fixes valid ones and explains the rest. Require CI on the current tested combination, not a prior push. Run live E2E last in an authorized test manager. Record success only after the run finishes and cleans up.

At merge, the broker rechecks the approved revision, pending redesigns, scope, reviews, checks and head/base compatibility. Do not register a design revision and merge a story concurrently. Require up-to-date checks in GitHub and pass the expected head SHA to the squash request. If base, head, scope or evidence changes or is unknown, stop or rerun validation. Test these races before enabling merges. Verify and record the squash SHA; it is not the pre-merge SHA CI tested.

```mermaid
sequenceDiagram
    participant B as Builder
    participant G as GitHub PR
    participant C as Trusted CI and review
    participant E as E2E launcher
    participant X as Merge broker
    B->>B: Red, green, refactor
    B->>G: Push candidate and open story PR
    G->>C: CI and PR-Agent for exact candidate
    C-->>G: Required evidence and findings disposition
    G->>E: Eligible checked candidate, E2E last
    E->>E: Acquire manager lock; run and clean up
    E-->>X: Trusted E2E outcome bound to candidate
    X->>G: Recheck revision, reviews, checks and candidate
    alt Evidence current and all gates pass
        X->>G: Squash with expected head and explicit trailers
        X->>G: Verify merge SHA and issue closure
    else Changed, blocked or unknown
        Note over X: No merge; reconcile, rerun or route to design
    end
```

Require the E2E lock for manual and automated runs, from the initial sweep through final cleanup. The current suite sweeps all `floe-e2e-*` names [#33][s33]. Random names do not help, and a tick limit cannot protect manual runs. Put the lock in the trusted launcher or test harness, outside candidate control. After a crash, a free lock does not prove cleanup. Block new runs until the launcher checks the prior process group and owned units and confirms cleanup. Independently of the lock, keep candidate code away from host resources, other managers, and personal or production credentials.

## Sessions and closeout

Use persistent `pi --print --session-id … --name …`, not `--no-session`. Keep the session ID and its private storage location. Set access, retention, recovery and deletion policies before rollout. If using Durable, record the storage location and conversation ID, not just the provider session ID. Use a reader for that format on finished snapshots. The harness already captures sessions. Neither `pi --export` HTML nor Durable entry dumps are public artifacts.

**Resolve D-07 before releasing dialogue.** The recommended amendment keeps sessions private, links them by ID, and publishes only a safe index and purpose-written summary. It keeps D-04's single approval. Until the maintainer resolves the conflict, do not publish dialogue. If the maintainer retains dialogue publication, either require a privacy review of the exact bytes before push (a second approval in substance) or explicitly accept scanner-only risk (not recommended). Record any permitted omissions in the index without exposing private details.

If the maintainer approves dialogue, publish only canonical user and assistant text JSONL under `transcripts/`. Omit tool data, thinking, metadata, attachments and context-edited targets. Reject unsupported sources. Redact credential shapes and private paths. Start with a 2 MiB uncompressed budget per feature.

Finish the listed sessions before freezing the index. Record the Archivist session ID, finish its work and send the sealed artifact to the publisher without starting another model session. Keep raw session access in private export. For all closeout artifacts, enforce the approved schema, path and redaction rules. Use pinned, checksum-verified Gitleaks with trusted rules including the Bearer control. Scan the exact serialized bytes and, where applicable, the decoded text.

On findings, errors, unverified bytes, or invalid or oversized input, hold the candidate privately. Report only safe status to the maintainer. Fix the issue and rescan; never waive a finding or publish reports or matching lines. Only the publisher can release the exact verified bytes. Hosted CI must check the same candidate using trusted policy, not candidate code. CI is a backstop: a push to a public branch has already disclosed the bytes [#35][s35].

```mermaid
sequenceDiagram
    participant A as Archivist / private staging
    participant P as Trusted publisher
    participant G as Public GitHub
    participant C as Trusted CI
    A->>A: Reconcile finished sessions, build and seal allowed artifact
    A->>P: Submit immutable candidate under approved policy
    P->>P: Verify policy, schema, redactions and scan exact bytes
    alt Finding, error or required privacy review absent
        P-->>A: Hold privately; report safe blocker only
    else Verified and authorized for public release
        P->>G: Push those exact bytes; PR Closes feature
        G->>C: Check exact candidate with independent policy
        C-->>G: Required result
        P->>G: Gated squash; verify commit and closure
    end
```

## Decisions requested from the maintainer

All recommendations are **Proposed**. Resolve them in the design PR before planning.

| Choice | Options, evidence and agent recommendation |
| --- | --- |
| Runner | **Persistent Pi CLI worker per issue attempt** in a separate unit, or a pi-durable server over authenticated HTTP. Both do basic coding [#31][s31]. Durable can recover work and prevent duplicate starts within a conversation. It also adds a host, tool policy and exporter, and still cannot tell whether an external write landed [#32][s32]. Revisit if losing in-progress turns becomes costly. Use native Durable tools if switching. |
| Approval and identities | **Validate App + protected canonical ADR on `master`**, or an authoritative maintainer-only ref if the split fails or only a human may press Merge. The latter complicates auto-closure and revision pinning. Shared login, same-user token, labels and workflow instructions are not enforcement [#34][s34]. |
| D-07 with D-04 | **Amend D-07 to private sessions plus public index and summary**, keeping D-04. Alternatives: review exact dialogue bytes privately before each release (amend D-04), or accept scanner-only dialogue risk explicitly (not recommended). Raw publication is not recommended under any option [#35][s35]. |
| E2E serialization | **Require a per-manager lock now** in a dedicated test environment. Sweeping only proven owned fixtures would reduce cleanup risk, but would not prove parallel tests safe. Do both if concurrency warrants it, and keep recovery for stale units. Separate managers could isolate parallel runs later [#33][s33]. |
| Code location | **Repository-local development tooling outside `src/`** for the first consumer, deployed from a pinned, protected snapshot. Separate reusable repo gives stronger lifecycle separation but adds cross-repo releases and bootstrap. No comparative spike; extract after a second consumer establishes a stable boundary. |
| Machine | **One dedicated Linux automation host or VM**, with separate control and test identities/managers, not the maintainer's everyday session. Existing workstation is cheap but couples sessions, credentials and sweeps; split hosts add operations and transport. Confirm target machine, isolation and unattended timer availability; #33 only proved the local wheel path. |
| Parallelism | **One active worker initially, serial stories**, with explicit prerequisites and a separate E2E lock even at capacity one. Bounded parallel spikes or independent stories later may save time but need race, capacity and rebase evidence; #33 measured a duplicate-admission race, not throughput. |
| Models by role | **Pin one capable model for Intake, Spike, Designer, Planner, Builder and any Archivist prose initially**; use no model for the tick, policy checks or merge. Role-specific cheaper models may help after measured quality/cost comparisons. #31 compared runners with one model, not models; maintainer selects the actual model and spend limit. |
| Decision log | **Start the file in the feature artifact directory in the design PR**, preserving #29's table as requirements provenance. Keeping everything on the issue makes later design/decision diffs harder to review; split logs break the feature history. |

### Alternatives not recommended

A tick that waits ties worker lifetime to the timer service. A separate workflow database duplicates issue and PR state without eliminating external checks. Webhooks can reduce delay but add a receiver and still need recovery for missed events. Neither a timer-only lock nor a GitHub run note prevents a duplicate start after a lost reply [#33][s33]. Keep the short polling tick and let the launcher decide whether to start a new worker.

## Validation and backlog seeds

These are candidate Planner stories, not issues. Start with synthetic/throwaway evidence; leave production writes disabled until the corresponding gates pass.

| Order | Candidate behavior | Acceptance check / release gate |
| --- | --- | --- |
| 1 | Reconstruct readiness from external evidence. | Fixtures exercise pagination, duplicate candidates, outages, premature closure, shadow/deleted ADR, superseded scope, pending revision and replan. Unknown blocks affected work while an independent item progresses. |
| 2 | Admit or recover one worker per attempt. | Race starts at the same key; lose accept/writeback replies; kill launcher before/after reservation, spawn and receipt; restart and collect completed units. No second worker or capacity leak. Changed payload, unknown state and wrong-owner cancellation block. Inject uncertain mutations and demonstrate external-state checks, not blind replay. |
| 3 | Enforce repository approval and identity policy. | **Get authorization to create the App and throwaway repo.** Show an App-authored story squashes without human review and a design squashes only with valid owner approval. Reject stale or wrong-actor reviews, unapproved mixed-path PRs, direct pushes, unapproved ADR rename or policy replacement, and failed, pending or wrong-source checks. Verify App-only identity and narrow role access. Check sub-issue and Development links, trailers and closure after squash and branch deletion, and recovery after a lost merge reply. Get separate authorization for production App installation and repo or ruleset changes. |
| 4 | Run the deployed tick and worker independently. | **Explicit authorization required before installing units or modifying the user's systemd session**; prefer an approved disposable manager. Verify timer activation, concurrent manual starts joining the tick, short timeout/local child cleanup, worker survival past tick exit/timeout, restart and cancellation. Verify noninteractive credentials in the real service environment without printing secrets, and continued operation with a broken Builder checkout. |
| 5 | Gate stories on exact-candidate results and safe live E2E. | Document D-05's standing scope before unattended execution and validate on an authorized disposable test manager. Two callers including the manual entry cannot overlap sweeps; unrelated units aren't touched, and failed cleanup blocks reuse. Exercise stale review, head/base changes during merge, missing PR-Agent, stale E2E and pending redesign; none may merge. Prove a matching checked candidate merges and its distinct squash SHA is recorded. |
| 6 | Seal and publish only allowed closeout bytes. | Use synthetic sessions/artifacts; test clean, seeded, malformed, oversized, unsupported and scanner-failure cases, candidate swaps and renamed/misplaced files. Verify no public push on holds, trusted hosted check attached to exact candidate, and closeout PR alone closes the feature. If dialogue ever becomes authorized, validate each chosen source format, privacy review policy and both scanner views before real publication. |
| 7 | Prove the lifecycle in a bounded pilot. | In the approved environment, follow a small request through design approval, a story, a blocked redesign/replan and closeout. Navigate issue → session ID → evidence → PR → squash SHA after topic branch deletion. Exercise pause, revocation and manual recovery before opening production admission. |

## Consequences and remaining unknowns

GitHub outages pause progress. Expect rate limits, delays before writes become visible, and outcomes that an operator may have to resolve. CLI loses automatic recovery within a model turn; Durable adds storage, replay and subprocess risks. Worker completion records, boot recovery, credential delivery, session retention, and exact permission and CI bindings still need validation.

A lock reduces throughput and cannot isolate code. Automated review cannot prove that a story follows the design. Stop the affected feature if evidence is missing or the implementation contradicts the design. Withholding dialogue leaves a less complete public record but avoids irreversible disclosure based on a scanner pass. Leave any production capability disabled if its safety gate cannot be proven.

## References: observations vs. design inferences

| Source | Observed | Inference used here / limitation |
| --- | --- | --- |
| [#31 pinned result][s31] | Both Pi CLI and pi-durable 1.5.2 made the same fixture fix; native Durable tools worked; CLI no-session left no file. | CLI is a reasonable baseline, not proven parity on realistic tasks or authenticated GitHub. Durable retrieval isn't a publishable transcript format. |
| [#32 pinned result][s32] | Same-key Durable submission deduped across reopen within one conversation. Both pre-body and post-push crashes returned `interrupted`; `safe` replay duplicated a marker. | Separate admission from effects. No live GitHub merge, multi-writer or surviving subprocess proof. |
| [#33 pinned result][s33] | Wheel tick and exact rendered command worked with a poisoned checkout; forced race needed runner dedupe; stub survived lost reply and tick death. | Pin tick deployment and require durable admission. Installed scheduling, service credentials and cgroup lifecycle remain untested; E2E sweep behavior is source analysis. |
| [#34 pinned result][s34] | Read-only hierarchy and protection inventory, PR association after branch deletion, explicit trailer preservation in local squash control. | GitHub can be the index; the proposed live App/CODEOWNERS split and closing-link behavior still need write tests. |
| [#35 pinned result][s35] | Pinned scanner with Bearer rule caught six synthetic secrets in decoded text; serialized JSONL missed one. Local CI step rejected dirty and malformed inputs. No real session was declared safe. | Dual-view gating helps, not a privacy guarantee; hosted CI and Durable normalization remain untested. |
| [#30 orchestrator review][review] | Reviewed spike scope and handoffs. | Separate worker cgroup is a lifecycle inference from the generated service, not an installed-service result. |

[feature]: https://github.com/ipdelete/floe/issues/29
[design]: https://github.com/ipdelete/floe/issues/30
[review]: https://github.com/ipdelete/floe/issues/30#issuecomment-7024844380
[s31]: https://github.com/ipdelete/floe/blob/fb7d832d0d3db6605f53cc484d0b33d114fbb558/.agents/engineering/29-agentic-dev-loop/experiments/pi-durable-coding/RESULTS.md
[s32]: https://github.com/ipdelete/floe/blob/3b819d8a30d7f51b65058e588edd2e8ea3a2b09d/.agents/engineering/29-agentic-dev-loop/experiments/durable-resume/RESULTS.md
[s33]: https://github.com/ipdelete/floe/blob/5ac08a5e3a2bd2fe75c079ac8d957f133c126508/.agents/engineering/29-agentic-dev-loop/experiments/floe-orchestrator/RESULTS.md
[s34]: https://github.com/ipdelete/floe/blob/e861ba4b312ffe7079b9e231735a323357a5f485/.agents/engineering/29-agentic-dev-loop/experiments/github-control/RESULTS.md
[s35]: https://github.com/ipdelete/floe/blob/642c958f92c15469156e36a19015dec59eb0ae67/.agents/engineering/29-agentic-dev-loop/experiments/transcripts/RESULTS.md
