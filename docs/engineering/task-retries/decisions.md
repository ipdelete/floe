# Task retries

Status: implemented; PR review and CI pending. Entry point for issue [#6](https://github.com/ipdelete/floe/issues/6).

## Approved direction

The maintainer approved the agent's recommendations in this conversation before implementation:

- Tenacity is the sole approved third-party runtime dependency. Keep sequential execution.
- Optional per-task `retries` mapping with `RetryPolicy(max_attempts, delay_seconds, retry_on)`; no workflow defaults, backoff, jitter, or elapsed-time retry budgets in v1.
- Explicit retryable exception types; retry only task-call failures. Never retry interrupts, cancellation, callbacks, or Floe synchronous-return validation.
- Services use the installing interpreter's absolute path, preserving virtualenv symlinks. No environment provisioning or package installation at service start. Environment lifetime is the user's responsibility.
- Distinguish task and attempt lifecycles; preserve failed attempts and output attribution, and read old history without inventing attempts.
- Show current attempt/retry waits and eventual success in CLI/dashboard.

Alternatives: system Python cannot reliably import Tenacity; Floe-managed environments add provisioning/cleanup scope. Workflow defaults and direct Tenacity objects expand the public API unnecessarily. Broad default exception matching hides programming errors.

## Implementation design

Retry-enabled workflows emit schema 2 with attempt-start/completion and retry-scheduled events. Unconfigured workflows retain schema 1 events. Readers accept both, validating new fields. Attempt identity is `(run_id, task, attempt)`; task completion records exhaustion versus non-retryable failure. Missing completion remains incomplete, not exhausted. Existing JSON gains additive attempt fields; exact-key consumers must adapt.

Tenacity's explicit attempt iterator surrounds only the task call. Events, synchronous-return validation, and waits remain outside its exception-catching context. Systemd bounds deployed attempts and waits with its existing workflow timeout; direct runs still have no overall timeout. In-process state persists across attempts. Sibling isolation is reactivated by the existing loader wrapper for each call.

Validation: deterministic waits, failure/cancellation/callback isolation, old/new/malformed and truncated history, CLI/web presentation, clean wheel installation and generated interpreter command, offline systemd unit verification, full normal tests and deployment demo. Real-session E2E is not authorized.

## Validation evidence

- Normal suite: 581 passed; 15 opt-in real-systemd cases skipped, including two new retry lifecycle cases.
- Deployment demo and whitespace checks passed; built a wheel through the source distribution.
- Packaging regression installs the wheel and cached Tenacity into a clean environment outside the checkout, runs its console entry point and exact generated service command with a retrying workflow, and verifies units offline.
- Responsive browser fixtures: success, waiting, and interrupted views at 320/390/768/1440px; attempt disclosures work and no page-level overflow. Inspected mobile success and desktop waiting renders. Mechanical design detector reported no findings.
- Disposable mutation probes detected an off-by-one attempt limit, ignored fixed delay, and lost attempt-output attribution. No mutations were retained.

Uncertainty: actual systemd lifecycle behavior remains unverified without separately approved real-session tests. Browser validation used synthetic journal fixtures, not the user's session.
