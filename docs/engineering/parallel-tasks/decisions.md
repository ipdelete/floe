# Decisions: parallel tasks

## D-001: Do not advance ordinary thread-pool workers to production design yet

- **Session:** thread-worker spike for [#9](https://github.com/ipdelete/floe/issues/9),
  parent [#8](https://github.com/ipdelete/floe/issues/8).
- **Status:** Proposed; agent recommendation, awaiting maintainer judgment.
- **Question:** Can ordinary in-process thread workers preserve Floe's supported
  import, output, failure, and lifecycle contracts with useful concurrency?
- **Recommendation:** Stop this thread investigation after the contract probes;
  retain sequential production execution and investigate processes separately.
  Do not implement a threaded scheduler or weaken contracts to rescue this spike.
- **Alternatives:**
  - Reuse the current task wrappers concurrently: observed wrong sibling imports
    and broken restoration, including after an exception.
  - Lock the whole import-active task call and descriptor-routing interval:
    protects those global critical sections, but serializes the tasks themselves.
    This consequence follows from the lock scope; no lock-performance measurement
    was conducted.
  - Activate one workflow's imports around the entire run, replace task wrappers,
    and use Python thread-local output routing: potentially addresses same-workflow
    wrapper nesting and Python output, but does not attribute arbitrary descriptor
    writes or inherited child output. Not implemented or approved.
  - Restrict workflows to explicit attributed logging/subprocess APIs, forbid
    relevant global-state mutations, and require cooperative cancellation:
    compatibility changes requiring separate maintainer judgment; not selected.
  - Investigate process workers: separate descriptor/interpreter state is a
    plausible alternative, not a proven solution or approved execution model.
- **Evidence:** [Thread spike](threads-spike.md), including the checked-in
  reproducible probes, sequential controls, failure cases, and limitations.
- **Uncertainty:** Not a proof that every possible Linux thread architecture is
  impossible. No new import architecture, instrumentation of arbitrary writes,
  restricted task API, free-threaded build, process approach, or real-systemd
  lifecycle was evaluated. Cancellation of a running future is not termination,
  but whole-process timeout termination remains a separate systemd concern.
- **Approval:** None for an execution model or compatibility change. The user
  authorized the spike and explicitly declined the proposed timebox; that is
  not approval of this recommendation.

## Session reconciliation

D-001 records the recommendation made after observing the probes, not an accepted
architecture. No production files or public contracts changed. Remaining work:
maintainer disposition of the thread recommendation, process spike #10, and an
approach comparison before any production design. Unrun scenarios are listed in
the spike; no inferred result is presented as an executed test.
