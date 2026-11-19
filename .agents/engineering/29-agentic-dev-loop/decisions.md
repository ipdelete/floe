# Decisions: agentic development loop

[Feature #29][feature] · [Design #30][design] · [Accepted ADR](design.md). This log spans requirements, spikes, design and implementation. Only the requirements rows are recorded decisions. The proposals do not supersede them unless the maintainer approves the change and the design PR records it. Design PR: [#36](https://github.com/ipdelete/floe/pull/36). The maintainer approved it in the orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` and instructed the agent to merge it; the merge SHA is recorded on #29 and #30.

## Requirements — transcribed from #29

Every row's phase, decision maker and wording come from [#29's decision table][requirements], not a new approval. `R` is the requirements session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` (the issue uses `01a1f52b`). Status and evidence columns are the log's annotations. D-09 and D-10 were agent decisions, not maintainer approvals.

| ID | Decision | Phase | Decided by | Status | Issue / session | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| D-01 | One loop; the maintainer stays at the decision points and agents run the rest | Requirements | Maintainer | Accepted | [#29][feature] / R | [Requirements][requirements] |
| D-02 | The feature issue is the index; IDs combine the issue number and a slug | Requirements | Maintainer | Accepted | [#29][feature] / R | [Requirements][requirements] |
| D-03 | The decision log spans the whole feature and records agent decisions | Requirements | Maintainer | Accepted | [#29][feature] / R | [Requirements][requirements] |
| D-04 | The design PR is the only human approval | Requirements | Maintainer | Accepted | [#29][feature] / R | [Requirements][requirements] |
| D-05 | TDD for stories, with live E2E per story, run last; standing approval for `floe-e2e-*` units | Requirements | Maintainer | Accepted | [#29][feature] / R | [Requirements][requirements]; repository instructions still need updating before unattended use |
| D-06 | Design changes go back through a design PR, then the backlog is adjusted | Requirements | Maintainer | Accepted | [#29][feature] / R | [Requirements][requirements] |
| D-07 | Sessions are published to this public repo at closeout, after redaction and a fail-closed secrets check | Requirements | Maintainer | Amended by D-13 | [#29][feature] / R | [Requirements][requirements]; amended by D-13 |
| D-08 | Scripting the deterministic steps is deferred | Requirements | Maintainer | Accepted | [#29][feature] / R | [Requirements][requirements] |
| D-09 | Use the existing `enhancement` label rather than creating a "Feature" label; spike and design issues use title prefixes instead of labels | Requirements | Agent | Accepted | [#29][feature] / R | [Requirements][requirements] |
| D-10 | Five spikes, chosen as the uncertainties most likely to change the design: pi-durable coding ability, crash-resume safety, Floe as orchestrator, GitHub state and permissions, transcript publication | Requirements | Agent | Accepted | [#29][feature] / R, #31–#35 | [Requirements][requirements] and spike handoffs below |

## Spike proposals — recorded at handoff to #30

Grouped by spike; the pinned results retain the full rationale and limitations. None is approved by closing the corresponding spike.

| ID | Recommendation and trade-off | Phase | Decision maker | Status | Issue / session | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| S-31 | Use persistent Pi CLI as baseline; use native tools if selecting Durable, adapting a CLI tool only for a proven gap. Record the private locator plus session/conversation ID and use a format-specific reader, not stock export as sanitization. Durable worked on the fixture but adds host and export work. | Spike | Agent recommendation | Proposed | [#31](https://github.com/ipdelete/floe/issues/31) / `01a1f603-1017-764e-903b-5359459e6112` | [Pinned result][s31] · [handoff][h31] |
| S-32 | Persist the attempt/conversation mapping and start key; reject conflicting payloads and recover pending work at boot. Keep mutations non-replayable; reconcile exact external effects and use `safe` only with repeatability evidence. Separate tool outcomes from submission status. Same-key recovery is not exactly-once effects. | Spike | Agent recommendation | Proposed | [#32](https://github.com/ipdelete/floe/issues/32) / `01a1f603-19a8-73e1-a282-efc93444db19` | [Pinned result][s32] · [handoff][h32] |
| S-33 | Pin the wheel and tick/config outside worktrees with absolute tools and least-privilege credentials. Bound ticks and isolate item failures; dedupe durably at admission and reconcile writeback. Lock live E2E at the manager or isolate managers; prefer fixture-owned cleanup if redesigning the harness. Stub results don't establish installed behavior. | Spike | Agent recommendation | Proposed | [#33](https://github.com/ipdelete/floe/issues/33) / `01a1f603-2301-72d6-88a2-caf294e3d91b` | [Pinned result][s33] · [handoff][h33] |
| S-34 | Use hierarchy plus PR/commit evidence and explicit closing keywords/squash trailers. Validate an isolated, no-bypass App and CODEOWNERS on `master`; use a separate authoritative ref only if needed. No unattended merges before positive and negative acceptance tests. | Spike | Agent recommendation | Proposed | [#34](https://github.com/ipdelete/floe/issues/34) / `01a1f603-2f49-70c9-ba29-68e1888c117e` | [Pinned result][s34] · [handoff][h34] |
| S-35 | Keep raw sessions private; if publishing dialogue, use canonical JSONL, a 2 MiB feature budget, pinned dual-view Gitleaks and an independent exact-candidate CI backstop. Make a separate pre-push privacy decision or withhold dialogue/publish a summary. Scanner results alone don't establish privacy. | Spike | Agent recommendation | Proposed | [#35](https://github.com/ipdelete/floe/issues/35) / `01a1f603-3947-76f3-8d83-d4048d3d2b61` | [Pinned result][s35] · [handoff][h35] |

## Design proposals — awaiting the design PR

`A` is the [design session on #30][session], `01a1f615-230c-70d5-8733-7540ddadf081`. The options, rationale and validation gates are in the [ADR](design.md). The ADR applies these recommendations together; it does not mark them accepted.

| ID | Choice and consequence | Phase | Decision maker | Status | Issue / session | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| A-01 | Prefer persistent CLI workers in separate per-attempt units to a pi-durable HTTP host. Accept loss of in-turn recovery; validate the independent service lifecycle rather than spawning children in the tick cgroup. | Design | Agent recommendation | Accepted as D-12 | [#30][design] / A | [#31][s31], [#32][s32], [orchestrator review][review] |
| A-02 | Use a pinned short Floe tick, GitHub evidence as feature state and a private durable admission ledger. Reconcile ambiguous effects rather than replaying them; unknown state blocks the item. | Design | Agent recommendation | Proposed | [#30][design] / A | [#32][s32], [#33][s33], [#34][s34] |
| A-03 | Use a protected canonical ADR on `master` and an isolated no-bypass App, with manual maintainer approval/merge for bootstrap and no unattended story merges until live acceptance. Separate-ref fallback needs its own approval. | Design | Agent recommendation | Superseded by D-11 | [#30][design] / A | [#34][s34], [orchestrator review][review] |
| A-04 | Amend D-07 to private native sessions plus public IDs/index and purpose-written summary, preserving D-04. Withhold dialogue rather than relying on a scanner as privacy review or silently adding a second approval. Seal and scan public artifacts before push; require an independent CI backstop. | Design | Agent recommendation | Accepted as D-13 | [#30][design] / A | [#35][s35], [orchestrator review][review] |
| A-05 | Lock the entire live E2E phase at a dedicated user-manager boundary. Consider narrowing cleanup later; neither a narrower sweep nor serial story scheduling alone constitutes mutual exclusion. | Design | Agent recommendation | Accepted as D-15 | [#30][design] / A | [#33][s33] |
| A-06 | Keep the first loop's code in repo-local development tooling, outside Floe runtime and deployed from a protected snapshot. Extract only after a reuse case. | Design | Agent recommendation | Accepted as D-16 | [#30][design] / A | [#29 open questions][questions], [#33][s33]; no comparative repository test |
| A-07 | Start on one dedicated Linux host/VM with separate test and control identities/managers, not the maintainer's ordinary session. Actual host and isolation require validation and authorization. | Design | Agent recommendation | Accepted as D-14 | [#30][design] / A | [#29 open questions][questions], [#33][s33]; feasibility only, no host chosen |
| A-08 | Admit one worker initially and serialize stories. Revisit with measured throughput and concurrency safety; keep the manager lock independently. | Design | Agent recommendation | Accepted as D-17 | [#30][design] / A | [#29 open questions][questions], [#33][s33] |
| A-09 | Start with one pinned capable model for reasoning/prose roles and none for orchestration or policy gates. Maintainer chooses model and budget; role-specific models need comparisons. | Design | Agent recommendation | Accepted as D-18 | [#30][design] / A | [#29 open questions][questions], [#31][s31]; no model-quality comparison |
| A-10 | Put the feature-wide decision log next to the ADR and retain the issue decision table as provenance. Record later approvals and changes with sessions and PRs. | Design | Agent recommendation | Accepted as D-19 | [#30][design] / A | [#29 open questions][questions], [engineering knowledge](../README.md#artifacts-and-decisions) |
| A-11 | Bootstrap exception: the maintainer's merge of design PR #36 under their own login is the design approval. GitHub can't record a self-approving review and doesn't yet enforce the gate (no App, CODEOWNERS or ruleset), so this approval is a recorded manual decision, not a server-enforced one. Later design revisions use the validated App gate. | Design | Agent recommendation | Superseded by D-11 | [#30][design], [#36](https://github.com/ipdelete/floe/pull/36) / A | [#34][s34]; PR-Agent review of #36 |

## Design decisions — maintainer

| ID | Decision | Phase | Decided by | Status | Issue / session | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| D-11 | Agents use the maintainer's own `gh` login on the maintainer's repos; no GitHub App or throwaway repo for now. The maintainer approves a design by merging its PR, and the loop's merge step never merges design PRs. GitHub does not enforce this split; revisit with a separate identity if unattended merging needs it. Supersedes A-03 and A-11. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer's reply during review of #36; [#34][s34] |
| D-12 | Run agents as persistent Pi CLI workers, one per issue attempt, each in its own systemd unit outside the tick's control group; not a pi-durable HTTP server. Accepts A-01. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer accepted the agent recommendations during review of #36; [#31][s31], [#32][s32] |
| D-13 | Amend D-07: keep raw sessions private and link them by ID; at closeout publish a safe index and a purpose-written summary, not dialogue. Keeps D-04's single approval. Accepts A-04. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer accepted the agent recommendations during review of #36; [#35][s35] |
| D-14 | Run the loop on one dedicated Linux host or VM, separate from the maintainer's everyday session, with separate control and test managers. Accepts A-07. | Design | Maintainer | Superseded by D-20 for now | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer accepted the agent recommendations during review of #36; [#33][s33] |
| D-15 | Run live E2E in a dedicated test systemd manager behind a mandatory lock covering setup through cleanup. Refines where D-05's standing approval applies. Accepts A-05. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer accepted the agent recommendations during review of #36; [#33][s33] |
| D-16 | Keep the loop's code in this repo as development tooling outside `src/`; extract to its own repo once a second project uses it. Accepts A-06. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer accepted the agent recommendations during review of #36; [#33][s33] |
| D-17 | Admit one worker at a time and run stories in order to start. Accepts A-08. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer accepted the agent recommendations during review of #36; [#33][s33] |
| D-18 | Use one pinned model, `github-copilot/gpt-6.2-sol`, for all reasoning roles and none for the tick, policy checks or merges; the maintainer sets a spend limit before the pilot. Accepts A-09. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer accepted the agent recommendations during review of #36; [#31][s31]; maintainer's model instruction during spikes |
| D-19 | Keep the feature-wide decision log as `decisions.md` beside the ADR, with #29's table as provenance. Accepts A-10. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer accepted the agent recommendations during review of #36; [engineering knowledge](../README.md#artifacts-and-decisions) |
| D-20 | Use the current machine (the maintainer's WSL2 workstation) for validation and initial operation, including installing the loop's own units in the maintainer's user session. Until a dedicated test manager and lock exist, live E2E runs one at a time in the maintainer's session under D-05. Supersedes D-14 for now; refines D-15. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer's go-ahead when approving #36 |
| D-21 | Session retention is undecided, and there is no backup policy. Sessions stay in Pi's local storage; losing them is an accepted risk. Waives the ADR's pre-pilot retention requirement. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer's go-ahead when approving #36 |
| D-22 | Design approved. The maintainer approved PR #36 and instructed the agent to merge it under the maintainer's login, consistent with D-11. | Design | Maintainer | Accepted | [#36](https://github.com/ipdelete/floe/pull/36) / orchestrating session `01a1f52b-4f03-7480-9a47-e3525d5ed1bb` | Maintainer's instruction in the orchestrating session |

## Session reconciliation

- **Session:** A. Reviewed D-01–D-10 against #29; transcribed the spike recommendations as S-31–S-35 and recorded A-01–A-11 during design. No rationale attributed to the maintainer beyond what #29 records.
- **Open approvals:** none for the design beyond merging #36; D-11–D-19 resolved the open decisions. Design approval does not grant GitHub App/repo or user-manager setup permission and does not establish that the validation gates passed.
- **Implementation:** No implementation decisions yet. Preserve previous rows when accepting or superseding a decision and link the new record to the old one.

[feature]: https://github.com/ipdelete/floe/issues/29
[requirements]: https://github.com/ipdelete/floe/issues/29#decision-log
[questions]: https://github.com/ipdelete/floe/issues/29#open-questions-for-the-maintainer
[design]: https://github.com/ipdelete/floe/issues/30
[session]: https://github.com/ipdelete/floe/issues/30#issuecomment-7024853923
[review]: https://github.com/ipdelete/floe/issues/30#issuecomment-7024844380
[h31]: https://github.com/ipdelete/floe/issues/30#issuecomment-7024833805
[h32]: https://github.com/ipdelete/floe/issues/30#issuecomment-7024834943
[h33]: https://github.com/ipdelete/floe/issues/30#issuecomment-7024836316
[h34]: https://github.com/ipdelete/floe/issues/30#issuecomment-7024837448
[h35]: https://github.com/ipdelete/floe/issues/30#issuecomment-7024838479
[s31]: https://github.com/ipdelete/floe/blob/fb7d832d0d3db6605f53cc484d0b33d114fbb558/.agents/engineering/29-agentic-dev-loop/experiments/pi-durable-coding/RESULTS.md
[s32]: https://github.com/ipdelete/floe/blob/3b819d8a30d7f51b65058e588edd2e8ea3a2b09d/.agents/engineering/29-agentic-dev-loop/experiments/durable-resume/RESULTS.md
[s33]: https://github.com/ipdelete/floe/blob/5ac08a5e3a2bd2fe75c079ac8d957f133c126508/.agents/engineering/29-agentic-dev-loop/experiments/floe-orchestrator/RESULTS.md
[s34]: https://github.com/ipdelete/floe/blob/e861ba4b312ffe7079b9e231735a323357a5f485/.agents/engineering/29-agentic-dev-loop/experiments/github-control/RESULTS.md
[s35]: https://github.com/ipdelete/floe/blob/642c958f92c15469156e36a19015dec59eb0ae67/.agents/engineering/29-agentic-dev-loop/experiments/transcripts/RESULTS.md
