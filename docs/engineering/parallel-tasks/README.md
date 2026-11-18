# Parallel task investigation

Parent issue: [#8](https://github.com/ipdelete/floe/issues/8).
Floe production execution remains sequential. Nothing here authorizes changing
that contract or modifying the user's systemd session.

## Current conclusions

- [Thread spike (#9)](threads-spike.md): deterministic local probes show that
  overlapping existing task wrappers break sibling imports/restoration, while
  thread-local Python output routing cannot attribute raw descriptor writes or
  inherited child stdout. Cancellation of a running future does not terminate
  its task/child. The agent recommends stopping this ordinary thread-pool path
  under current contracts; maintainer judgment is pending.
- Process spike [#10](https://github.com/ipdelete/floe/issues/10): not conducted
  here. Processes are a candidate to investigate, not an accepted solution.
- [Decisions](decisions.md): recommendations, alternatives, uncertainties and
  approval status. No parallel execution model or compatibility change approved.

## Evidence and next decision

[Thread probes](thread-probes.py) preserve the small reproducible experiments,
not a production scheduler. The [coverage table](threads-spike.md#coverage-against-the-shared-evaluation-contract)
identifies unrun scenarios, including DAG scheduling, performance benchmarks and
real-systemd lifecycle behavior. No speedup or process/thread equivalence is claimed.

Investigate processes and compare findings before selecting an execution model
or writing a production design/implementation issue. If threads are reconsidered,
resolve import lifetimes and arbitrary output attribution first, or explicitly
seek approval for narrower workflow contracts.
