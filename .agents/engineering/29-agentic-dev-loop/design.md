# ADR: Agentic development loop

**Status: Proposed — awaiting maintainer approval via the design PR**

[Feature #29][feature] · [Design #30][design] · [Decision log](decisions.md)

**Recommendation:** Use a short Floe timer tick to reconcile GitHub issues, PRs and commits, and dispatch persistent Pi CLI workers in separately managed units. Keep the approved ADR on protected `master`; use an isolated GitHub App identity for automation only after proving that it can merge a checked story but cannot land an unapproved design. Start with one worker and serialize live E2E at the target user-manager boundary. At public closeout, publish an artifact index and a purpose-written summary, not session dialogue, unless the maintainer adopts a different publication policy. These are proposals, not approvals.

This keeps phase state where the maintainer already works and lets the loop wait without an active model session. Both runners fixed the same fixture [#31][s31]; pi-durable persists tool checkpoints, but those cannot say whether an interrupted external effect landed [#32][s32]. Use the simpler worker first and invest in admission, reconciliation, and publication controls either choice needs.

## Context and scope

A sentence becomes a feature issue; the maintainer clarifies requirements and approves the design. Agents turn the approved backlog into tested, merged changes, with evidence traceable from the feature issue to decisions and sessions. Use one lifecycle, not selectable modes. An approved revision is a new pass through the same design gate (D-06), not a second kind of approval. Bootstrap and capability checks are rollout constraints, not alternate lifecycle modes.

The loop consumes Floe as an ordinary installed workflow. No Floe runtime dependency or public API change, new project-management database, bespoke model tool suite, multi-host failover, or general scripting of deterministic work (D-08). The small admission ledger and trusted mutation and publication guards are safety mechanisms, not a second feature-state store or a general workflow framework. Normal repo safety rules still apply. The standing story E2E scope in D-05 needs to be documented in `AGENTS.md` before unattended use; it does not authorize installing the orchestrator or experimenting in the user's manager.

## Roles and handoffs

| Role | Responsibility and evidence |
| --- | --- |
| Intake | Interactive request and requirements interview. Create the feature index (`enhancement`), record the session, requirements and decision seed, and link spike and design sub-issues. |
| Spike | One question, issue, named session and branch per spike. Push and retain the branch, pin the result on the design issue, then close the spike. No merge PR. |
| Designer | Synthesize evidence into the ADR and decision log. Propose a design PR; revisions get their own design issues and PRs. Cannot approve a design. |
| Planner | Read the exact merged ADR revision. Create or reconcile linked, verifiable story issues with acceptance checks and explicit prerequisites. |
| Builder | One issue, worktree, branch and session per attempt. Red → green → refactor; resolve review findings and supply exact-candidate evidence. Report design contradictions instead of deciding them in a story PR. |
| Archivist | Reconcile the session/artifact index, prepare the closeout artifact privately and submit only policy-approved bytes. Raw capture stays in native harness storage. |
| Floe tick | Read external state, reconcile work and request bounded admissions; exit without waiting for agents, checks, reviews or approvals. It does not interpret a model's answer as completion. |

Hand off through artifacts, not shared conversation. A worktree protects edits from collisions, not credentials or host state. Keep model-driven workers away from the maintainer's home, SSH agent, keyring, Git helpers, manager socket and CI/issuer credentials. Use separate OS identities or validated isolation, narrow per-role operations and noninteractive model access. Do not equate an allowlist containing unrestricted `bash` with authorization. With GitHub Contents write covering both push and merge, use a trusted broker for protected writes/merge and narrow publication, not an unrestricted token in the model process. Treat issue text, code and session data as inputs, not authority to expand scope or privileges.

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
| Active work | Record role, attempt, stable run ID and session ID on the issue, with links to the work branch/PR when known. A linked branch is not a lease; an issue annotation is a projection of admission state. Unknown reads or conflicting candidates block affected work, not the whole tick. |
| Spike completion | Pinned result commit on the retained spike branch, design-issue handoff and closed spike issue, or an explicit disposition before design. |
| Design approval | Matching design PR to `master` merged through the gate, matching issue, and canonical ADR at that merge on protected history. Pin the approved revision and resolve its path from trusted configuration; a draft branch, shadow file, symlink, review alone or closed issue is not approval. |
| Story completion | Open, in-scope, unblocked stories are eligible only without active work or a pending design revision. Done means the expected PR merged and the issue closed; record its squash SHA. An early closure or missing auto-close is a discrepancy, not success. When revising, explicitly dispose of superseded stories and invalidate old approval-dependent evidence. |
| Closeout | All expected stories under the current approved revision are done or explicitly superseded; no unresolved design revision. The expected closeout PR and squash SHA plus feature issue closure establish completion. |

Use `<feature-number>-<slug>` in branch names, artifact paths and session names, and the child issue number and attempt where applicable. Name intake after assigning the feature ID. Link work branches in Development; keep pinned spike results and durable PR/squash links independent of branch lifetime. PR bodies use `Closes #child` for design/story and a nonclosing feature reference. Only closeout uses `Closes #feature`. Use terminal `Refs:` trailers on work commits and explicitly write them into the final squash message (feature and child where applicable); verify the resulting commit. Record phase, decision maker, status, issue/PR, evidence and session in the feature [decision log](decisions.md). Publish IDs and safe labels, not storage locators, raw logs or tool output.

## Tick, worker and recovery contract

- Deploy the tick and config separately from Builder worktrees, from versioned, worker-nonwritable paths, with a pinned non-editable Floe wheel and absolute client executable paths. Use a short whole-tick budget, bounded API calls and batches, backoff for outages and rate limits, and per-item error isolation. Timer activations are triggers, not durable queued jobs. The tick needs no model credentials.
- The generated oneshot service uses `KillMode=control-group`. The [orchestrator review][review] infers that workers left in its cgroup will be stopped when the tick exits; this was not live-tested. Do not background or detach a child of the tick. Use a separately managed launcher and unit per CLI attempt, or a separately managed pi-durable HTTP host whose worker subprocesses have bounded lifecycle and cleanup. Unit names are a collision check, not a durable ledger.
- Key admission by `(repo ID, feature ID, issue ID, role, attempt)`, with an immutable request fingerprint (including approved revision and input snapshot where relevant). Atomically reserve the key, session identity and stable run/unit identity before acknowledging. Reuse that key after timeout or missing writeback; reject conflicting payloads, and mint new attempts only by explicit retry policy after external reconciliation. A single trusted launcher serializes capacity and admission, owns the private ledger and fences conflicting writers. Keep terminal receipts after unit collection. Recover a reserved-but-not-launched run only when launch is proven absent; a gone unit with no receipt is not proof of no launch. On state loss or ambiguity not resolved by inspection, stop rather than reconstruct a new run from an empty ledger. With pi-durable, additionally persist the exact storage and conversation mapping, reuse `requestId` in that conversation and drive pending work on boot.
- Expose start-or-get, status by key/run ID and idempotent cancel to an authorized caller. Confirm cancellation, reap the owned unit/group and reconcile effects before releasing the slot or attempting replacement; cancellation does not undo remote mutations. Write issue annotations conflict-aware and never regress terminal state. Use a single logical publisher for those records and reread ambiguous updates; don't assume a GitHub comment API offers a compare-and-swap.
- For a push, issue/sub-issue creation, PR creation or merge, use a stable operation identity and expected state (e.g. exact ref/SHA, parent and marker, or PR/base/head). Before a mutation or a retry inspect the remote: exact result → record it; confirmed absent → revalidate preconditions; conflicting, in-flight or unknown → block and reconcile later. If an endpoint cannot safely distinguish a late first write from absence, don't auto-retry it. Do not replay mutations merely because the tool or agent reports failure. Keep Durable mutation tools non-replayable; allow `safe` only with repeatability evidence. Worker exit, HTTP success and Durable `done` are not completion evidence.

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

**Bootstrap before the App:** the existing login is the maintainer's and `master` lacks a design-review gate at the spike snapshot [#34][s34]. Do not give workers the personal login or delegate unattended merges. Agents can prepare local candidates; a trusted operator may submit topic branches and hand off PR creation. For #30, the maintainer explicitly reviews and merges the exact design candidate under their own identity; record that manual decision and the merge SHA. If the sole owner authored the PR, do not claim a GitHub approving review (GitHub blocks self-approval). This is a manual bootstrap of the initial ADR, not proof of a server-enforced gate. Until rollout tests pass, allow only supervised setup/validation; don't put story merges on autopilot.

**Steady state candidate:** dedicated App installation limited to the approved repo, with only needed Contents, PR and Issues scopes and checks/status read if needed. No administration, workflow editing, trusted-check publishing, bypass or fallback to personal `gh`. Keep the App key with the issuer and restrict role operations at the broker. Require PRs, CODEOWNERS and stale approval dismissal on `master`, zero generic approvals, required checks from a trusted source, and no bypass (including inherited/classic protections). Make the maintainer the only effective design owner; protect the canonical ADR family (including this `design.md`), CODEOWNERS and trusted gate/CI inputs themselves. Protect or relocate check logic outside ordinary story paths. A PR touching any protected design/policy path follows that gate even if labeled a story. Check the actual rules and actors, not a bot-authored status or an issue label. This grants the App the ability to press Merge **after** the owner approves, not to supply the approval. Path rules don't detect semantic drift in story code.

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

**Story gate:** trusted coordinator records the nondraft PR, head SHA, compatible base/merge candidate, ADR revision, test evidence and PR-Agent review of that head. Builder verifies findings, fixes valid ones and records why others don't apply. Required CI must pass on the current head/tested combination, not a prior push. Run live E2E last on that candidate, in an authorized test manager; it must finish **and** clean up before a trusted result is recorded. Recheck approved revision, absence of pending redesign, scope, reviews, checks and head/base compatibility at merge; serialize revision registration and story merge via the trusted broker and use server-side up-to-date required checks and a head-SHA-guarded squash. If base, head, scope or required evidence changes or is unknown, stop or rerun validation. Prove the race protections before enabling merges. The squash has a new SHA; verify and record it, don't call it the pre-merge SHA that CI tested.

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

The E2E lock is mandatory for every manual or automated entry to that target manager and covers the suite's initial sweep through final cleanup. Put enforcement in a trusted launcher/harness outside candidate control; use a dedicated test manager with no unrelated workloads or orchestrator units. Candidate test code must not reach personal or production credentials or other managers. After a launcher crash, a free lock alone is not proof of cleanup; quarantine the manager until the prior process group and owned units are reconciled. The current suite sweeps all `floe-e2e-*` names [#33][s33]; random prefixes and a cap in the tick don't protect manual runs. Keep other host access constrained independently of the lock.

## Sessions and closeout

Use persistent `pi --print --session-id … --name …` (not `--no-session`). Keep a private locator plus session ID, with approved access, retention, recovery and deletion policies to define before rollout. If Durable is chosen, use a storage locator and conversation ID distinct from the provider session ID, and a format-specific reader of finished snapshots. Native capture avoids re-recording; neither `pi --export` HTML nor Durable entry dumps are public artifacts. Finalize the sessions listed for closeout before freezing the manifest; record the Archivist session ID and freeze its work before a trusted publisher transmits the artifact without starting another model session.

**Recommended policy change:** explicitly revise D-07 in the design PR to publish a safe index and purpose-written closeout summary, with private sessions linked by ID. Keep D-04: no routine second human approval. Do not lift the hold on public dialogue simply because D-07 currently calls for it. If the maintainer instead retains dialogue publication, approve a policy that also addresses D-04: require pre-push privacy review of exact bytes (a second approval in substance), or explicitly accept the residual risk of scanner-only release (not recommended). Do not silently drop unsupported sessions from a dialogue export; any allowed withholding must be visible in the index without private details.

For any approved dialogue path, use canonical user/assistant text JSONL only under `transcripts/`; omit tool data, thinking, metadata, attachments and context-edited targets. Block unsupported sources, redact credential shapes and private paths, and use a 2 MiB uncompressed feature budget initially. For all closeout artifacts enforce the approved schema and path, redaction and pinned checksum-verified Gitleaks with trusted rules including the Bearer control. Scan exact serialized bytes and decoded text where applicable. Fail closed on findings, errors, unverified bytes, or invalid/oversize input. Maintain a private candidate and report only safe status to the maintainer on a finding; remediate and rescan, never waive a finding. Do not publish reports or matching lines. Limit raw session access to private export; only a trusted publisher can promote the sealed, verified bytes. Hosted CI must independently gate the exact candidate using trusted policy, not candidate code. CI is a backstop, not confidentiality: a public branch push already discloses its bytes [#35][s35].

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

Every recommendation below is **Proposed**. The design PR should resolve them before planning; setup authorizations and passing validation are separate from approval of the architecture.

| Choice | Options, evidence and agent recommendation |
| --- | --- |
| Runner | **Persistent Pi CLI worker per issue attempt** in a separate unit, or a pi-durable server over authenticated HTTP. Recommend CLI: both do basic coding [#31][s31]; Durable's same-conversation dedupe/recovery is useful but adds host/tool/export ownership without resolving external ambiguity [#32][s32]. Revisit if loss of in-progress turns is a measured cost; use native Durable tools if switched. |
| Approval and identities | **Validate App + protected canonical ADR on `master`**, or an authoritative maintainer-only ref if the split fails or only a human may press Merge. The latter complicates auto-closure and revision pinning. Shared login, same-user token, labels and workflow instructions are not enforcement [#34][s34]. |
| D-07 with D-04 | **Amend D-07 to private sessions plus public index and summary**, keeping D-04. Alternatives: review exact dialogue bytes privately before each release (amend D-04), or accept scanner-only dialogue risk explicitly (not recommended). Raw publication is not recommended under any option [#35][s35]. |
| E2E serialization | **Mandatory per-manager lock now** in a dedicated test environment. Narrowing the sweep to proven owned fixtures reduces cleanup risk but is not evidence that parallel tests are safe; do both eventually if concurrency warrants it, with recoverable stale-unit handling. Separate managers can isolate parallel runs later [#33][s33]. |
| Code location | **Repository-local development tooling outside `src/`** for the first consumer, deployed from a pinned, protected snapshot. Separate reusable repo gives stronger lifecycle separation but adds cross-repo releases and bootstrap. No comparative spike; extract after a second consumer establishes a stable boundary. |
| Machine | **One dedicated Linux automation host or VM**, with separate control and test identities/managers, not the maintainer's everyday session. Existing workstation is cheap but couples sessions, credentials and sweeps; split hosts add operations and transport. Confirm target machine, isolation and unattended timer availability; #33 only proved the local wheel path. |
| Parallelism | **One active worker initially, serial stories**, with explicit prerequisites and a separate E2E lock even at capacity one. Bounded parallel spikes or independent stories later may save time but need race, capacity and rebase evidence; #33 measured a duplicate-admission race, not throughput. |
| Models by role | **Pin one capable model for Intake, Spike, Designer, Planner, Builder and any Archivist prose initially**; use no model for the tick, policy checks or merge. Role-specific cheaper models may help after measured quality/cost comparisons. #31 compared runners with one model, not models; maintainer selects the actual model and spend limit. |
| Decision log | **Start the file in the feature artifact directory in the design PR**, preserving #29's table as requirements provenance. Keeping everything on the issue makes later design/decision diffs harder to review; split logs break the feature history. |

### Alternatives not recommended

The table above compares the consequential choices. For orchestration, a waiting tick couples worker lifetime to the timer service; a separate workflow database duplicates the issue/PR state and still needs external reconciliation. A webhook-driven service could reduce polling delay but adds a receiver and still needs recovery for missed events. A timer-only lock or a GitHub run annotation cannot dedupe admissions after a lost reply [#33][s33]. Keep the short polling tick and put each control at the boundary that can enforce it.

## Validation and backlog seeds

These are candidate Planner stories, not issues. Start with synthetic/throwaway evidence; leave production writes disabled until the corresponding gates pass.

| Order | Candidate behavior | Acceptance check / release gate |
| --- | --- | --- |
| 1 | Reconstruct readiness from external evidence. | Fixtures exercise pagination, duplicate candidates, outages, premature closure, shadow/deleted ADR, superseded scope, pending revision and replan. Unknown blocks affected work while an independent item progresses. |
| 2 | Admit or recover one worker per attempt. | Race starts at the same key; lose accept/writeback replies; kill launcher before/after reservation, spawn and receipt; restart and collect completed units. No second worker or capacity leak. Changed payload, unknown state and wrong-owner cancellation block. Inject uncertain mutations and demonstrate external-state checks, not blind replay. |
| 3 | Enforce repository approval and identity policy. | **Explicit authorization required to create the App and throwaway repo.** In that repo prove successful App-authored story squash with no human review, blocked design squash without owner approval, allowed reviewed design, stale/wrong-actor review rejection, blocked mixed-path PR/direct push/ADR rename or policy replacement, and failed/pending or wrong-source checks blocking. Verify App-only identity, narrow role access, sub-issue and Development links, trailers/closure after squash and branch deletion, and reconcile a lost merge reply. Separately authorize production App installation and repo/ruleset changes. |
| 4 | Run the deployed tick and worker independently. | **Explicit authorization required before installing units or modifying the user's systemd session**; prefer an approved disposable manager. Verify timer activation, concurrent manual starts joining the tick, short timeout/local child cleanup, worker survival past tick exit/timeout, restart and cancellation. Verify noninteractive credentials in the real service environment without printing secrets, and continued operation with a broken Builder checkout. |
| 5 | Gate stories on exact-candidate results and safe live E2E. | Document D-05's standing scope before unattended execution and validate on an authorized disposable test manager. Two callers including the manual entry cannot overlap sweeps; unrelated units aren't touched, and failed cleanup blocks reuse. Exercise stale review, head/base changes during merge, missing PR-Agent, stale E2E and pending redesign; none may merge. Prove a matching checked candidate merges and its distinct squash SHA is recorded. |
| 6 | Seal and publish only allowed closeout bytes. | Use synthetic sessions/artifacts; test clean, seeded, malformed, oversized, unsupported and scanner-failure cases, candidate swaps and renamed/misplaced files. Verify no public push on holds, trusted hosted check attached to exact candidate, and closeout PR alone closes the feature. If dialogue ever becomes authorized, validate each chosen source format, privacy review policy and both scanner views before real publication. |
| 7 | Prove the lifecycle in a bounded pilot. | In the approved environment, follow a small request through design approval, a story, a blocked redesign/replan and closeout. Navigate issue → session ID → evidence → PR → squash SHA after topic branch deletion. Exercise pause, revocation and manual recovery before opening production admission. |

## Consequences and remaining unknowns

GitHub outages now pause progress; the loop must tolerate rate limits, eventual visibility, and mutations with unknown outcomes. Retries may require operator reconciliation. CLI loses automatic in-turn recovery; Durable adds storage, replay and subprocess concerns. The runner's unit receipt, boot recovery, credential delivery, session retention and exact permission/CI bindings are open until tested. A lock reduces throughput and cannot isolate code by itself. Automated review cannot prove architectural fidelity; evidence gaps or design contradictions must halt the affected feature. Withholding dialogue reduces public fidelity but avoids irreversible disclosure based on a scanner pass. If any safety gate cannot be proven, leave the corresponding production capability disabled rather than weakening it.

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
