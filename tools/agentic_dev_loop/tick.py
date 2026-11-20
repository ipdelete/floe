"""One local Floe reconciliation tick over trusted fixture ports.

There is no network transport, model, worker lifecycle, or GitHub writer here.
A tick requests admission and mirrors the reservation, not worker progress.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice
from math import isfinite
from time import monotonic
from types import MappingProxyType
from typing import Protocol

from floe import Workflow

from .feature_tree import EvidenceReadError, IssueRef
from .launcher import AdmissionError, AttemptKey, LedgerUnavailable, Reservation, StartRequest, WorkerUnavailable
from .readiness import Decision, FeatureEvidence, Policy, decide_feature


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _finite_number(value: object) -> bool:
    return type(value) in (float, int) and isfinite(value)


@dataclass(frozen=True, slots=True)
class TickLimits:
    batch_size: int = 16
    tick_seconds: float = 10
    call_seconds: float = 2
    backoff_seconds: float = 30

    def __post_init__(self) -> None:
        if not _positive_int(self.batch_size):
            raise ValueError("batch size must be a positive integer")
        if not all(_finite_number(value) and value > 0 for value in (
                self.tick_seconds, self.call_seconds, self.backoff_seconds)):
            raise ValueError("tick, call and backoff durations must be positive and finite")


@dataclass(frozen=True, slots=True)
class FeatureScope:
    feature: IssueRef
    repo_id: int
    feature_id: int
    policy: Policy
    read_bucket: str
    write_bucket: str

    def __post_init__(self) -> None:
        if not _positive_int(self.repo_id) or not _positive_int(self.feature_id):
            raise ValueError("expected stable repository and feature IDs")
        if not all(isinstance(bucket, str) and bucket for bucket in (self.read_bucket, self.write_bucket)):
            raise ValueError("expected explicit read and write cooldown buckets")


@dataclass(frozen=True, slots=True)
class RunNote:
    reservation: Reservation
    terminal: bool = False  # The tick never sets or clears terminal status.


@dataclass(frozen=True, slots=True)
class AttemptInput:
    issue_id: int
    snapshot: bytes  # Immutable worker inputs; exclude volatile observations such as timestamps.
    note: RunNote | None  # None means confirmed absent; not a failed/ambiguous read.


@dataclass(frozen=True, slots=True)
class Observation:
    evidence: FeatureEvidence
    attempts: Mapping[int, AttemptInput]  # by issue number (not by stable ID)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempts", MappingProxyType(dict(self.attempts)))


class EndpointUnavailable(Exception):
    """The operation failed or its result is unknown; retry_at is absolute monotonic time."""

    def __init__(self, *, retry_at: float | None = None):
        if retry_at is not None and not _finite_number(retry_at):
            raise ValueError("retry_at must be a finite monotonic timestamp")
        super().__init__("endpoint unavailable")
        self.retry_at = retry_at


class IssuePort(Protocol):
    """Fixture issue state. All operations must stop waiting by the absolute deadline.

    Adapters share the tick's monotonic clock. Bucket identities in the trusted
    configuration must be shared by all scopes affected by the same outage or
    rate limit, and distinct for independent endpoints.
    """

    def read(self, scope: FeatureScope, *, deadline: float) -> Observation:
        """Reread current external evidence and corresponding attempt inputs.

        Only include an attempt when its ID, pinned inputs and note state are
        known. Raise EndpointUnavailable (with retry time on rate limits) for
        endpoint failure; represent other unknown evidence in the observation.
        """
        ...

    def mirror_if_missing(self, issue: IssueRef, note: RunNote, *, deadline: float) -> bool:
        """As the fixture's logical writer, reread and insert only if definitely absent.

        Return False for any existing note (matching, conflicting or terminal).
        An uncertain read or write raises EndpointUnavailable, not False. This
        operation is not a claim that GitHub provides conditional comment writes.
        """
        ...


class AdmissionPort(Protocol):
    bucket: str

    def start_or_get(self, request: StartRequest, *, deadline: float) -> Reservation:
        """Request admission and return stable IDs by deadline, not worker completion.

        Refuse conflicting fingerprints. Endpoint failure may hide a committed
        reservation; don't bypass the launcher to recover it.
        """
        ...


class Cooldowns:
    """Ephemeral endpoint backoff owned by the fixture host, not work state.

    Keep this instance across tick invocations in the same clock domain.
    """

    def __init__(self) -> None:
        self._until: dict[str, float] = {}

    def blocked(self, bucket: str, now: float) -> bool:
        if self._until.get(bucket, now) > now:
            return True
        self._until.pop(bucket, None)
        return False

    def defer(self, bucket: str, until: float) -> None:
        self._until[bucket] = max(until, self._until.get(bucket, until))


class Outcome(StrEnum):
    NO_WORK = "no-work"
    UNKNOWN_INPUT = "unknown-input"
    BACKOFF = "backoff"
    UNAVAILABLE = "unavailable"
    DEADLINE = "deadline"
    ADMISSION_REFUSED = "admission-refused"
    NOTE_PRESENT = "note-present"
    MIRRORED = "mirrored"


@dataclass(frozen=True, slots=True)
class FeatureResult:
    feature: IssueRef
    outcome: Outcome
    decision: Decision | None = None
    reservation: Reservation | None = None


class _Deferred(Exception):
    def __init__(self, outcome: Outcome):
        self.outcome = outcome


class Tick:
    def __init__(
        self, issues: IssuePort, launcher: AdmissionPort, cooldowns: Cooldowns, *,
        clock: Callable[[], float] = monotonic, limits: TickLimits = TickLimits(),
    ) -> None:
        if not isinstance(launcher.bucket, str) or not launcher.bucket:
            raise ValueError("expected an explicit launcher cooldown bucket")
        self._issues = issues
        self._launcher = launcher
        self._cooldowns = cooldowns
        self._clock = clock
        self._limits = limits

    def workflow(
        self, scopes: Iterable[FeatureScope], report: Callable[[tuple[FeatureResult, ...]], object],
    ) -> Workflow:
        """Snapshot a batch for a run-only workflow and a local, nonblocking report sink.

        Repeated runs check fresh evidence for the same scopes. Build a new
        workflow to select a different batch.
        """
        batch = tuple(islice(scopes, self._limits.batch_size))

        def reconcile() -> None:
            report(self.reconcile(batch))

        return Workflow("agentic-dev-tick", {"reconcile": reconcile}, {"reconcile": frozenset()})

    def reconcile(self, scopes: Iterable[FeatureScope]) -> tuple[FeatureResult, ...]:
        """Process at most one bounded batch from a trusted ordered scope list."""
        deadline = self._clock() + self._limits.tick_seconds
        results = []
        for scope in islice(scopes, self._limits.batch_size):
            if self._clock() >= deadline:
                break
            results.append(self._reconcile_feature(scope, deadline))
        return tuple(results)

    def _available(self, bucket: str, deadline: float) -> float:
        now = self._clock()
        if now >= deadline:
            raise _Deferred(Outcome.DEADLINE)
        if self._cooldowns.blocked(bucket, now):
            raise _Deferred(Outcome.BACKOFF)
        return now

    def _call[T](
        self, bucket: str, deadline: float, operation: Callable[..., T], *args: object,
    ) -> tuple[T, bool]:
        """Return the reply and whether it arrived in time to act on it."""
        until = min(deadline, self._available(bucket, deadline) + self._limits.call_seconds)
        try:
            result = operation(*args, deadline=until)
        except (EndpointUnavailable, OSError, EvidenceReadError, LedgerUnavailable, WorkerUnavailable) as exc:
            resume = self._clock() + self._limits.backoff_seconds
            if isinstance(exc, EndpointUnavailable) and exc.retry_at is not None:
                resume = max(resume, exc.retry_at)
            self._cooldowns.defer(bucket, resume)
            raise _Deferred(Outcome.UNAVAILABLE) from exc
        now = self._clock()
        if now >= until:
            self._cooldowns.defer(bucket, now + self._limits.backoff_seconds)
            return result, False
        return result, True

    def _reconcile_feature(self, scope: FeatureScope, deadline: float) -> FeatureResult:
        decision = reservation = None
        try:
            observation, timely = self._call(scope.read_bucket, deadline, self._issues.read, scope)
            if not timely:
                return FeatureResult(scope.feature, Outcome.DEADLINE)
            decision = decide_feature(scope.feature, scope.policy, observation.evidence)
            work = decision.work
            if work is None:
                return FeatureResult(scope.feature, Outcome.NO_WORK, decision)
            data = observation.attempts.get(work.issue)
            if not isinstance(data, AttemptInput) or not _positive_int(data.issue_id) or type(data.snapshot) is not bytes:
                return FeatureResult(scope.feature, Outcome.UNKNOWN_INPUT, decision)
            if data.note is not None:
                return FeatureResult(scope.feature, Outcome.NOTE_PRESENT, decision)

            # Don't admit new work when its writeback endpoint is already backed off.
            self._available(scope.write_bucket, deadline)
            key = AttemptKey(scope.repo_id, scope.feature_id, data.issue_id, work.role, 1)
            request = StartRequest(key, work.approval.sha if work.approval else None, data.snapshot)
            reply, timely = self._call(self._launcher.bucket, deadline, self._launcher.start_or_get, request)
            if not isinstance(reply, Reservation) or reply.key != key:
                return FeatureResult(scope.feature, Outcome.ADMISSION_REFUSED, decision)
            reservation = reply
            if not timely:
                return FeatureResult(scope.feature, Outcome.DEADLINE, decision, reservation)
            issue = IssueRef(scope.feature.repo, work.issue)
            # This is the last operation: report a confirmed result even if the
            # reply was late. A lost or ambiguous receipt remains UNAVAILABLE.
            written, _ = self._call(scope.write_bucket, deadline, self._issues.mirror_if_missing,
                                    issue, RunNote(reservation))
            if written is True:
                outcome = Outcome.MIRRORED
            elif written is False:
                outcome = Outcome.NOTE_PRESENT
            else:
                outcome = Outcome.UNAVAILABLE
        except _Deferred as exc:
            outcome = exc.outcome
        except AdmissionError:
            outcome = Outcome.ADMISSION_REFUSED
        return FeatureResult(scope.feature, outcome, decision, reservation)
