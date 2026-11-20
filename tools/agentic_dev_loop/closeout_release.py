"""Trusted closeout publication and merge gates. No live GitHub transport."""

import json
import sqlite3
from dataclasses import asdict, dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Protocol

from .closeout import FrozenCloseout, public_scope
from .closeout_policy import ReleaseStatus, SealedCloseout, delta_digest, release_closeout
from .feature_tree import EvidenceReadError, IssueRef, valid_issue_ref
from .github_writes import WriterBusy, locked_writer, request_fingerprint
from .handoffs import _feature_tag
from .hosted_closeout import Commit
from .readiness import FeatureEvidence, IssueState, Policy, PullRequestState, decide_feature, valid_sha
from .story_merge import _unsettled


@dataclass(frozen=True, slots=True)
class Target:
    feature: IssueRef
    slug: str
    revision: str
    base_sha: str

    @property
    def branch(self) -> str:
        return f'feature/{_feature_tag(self.feature, self.slug)}-closeout'


@dataclass(frozen=True, slots=True)
class Publication:
    target: Target
    commit: Commit

    @property
    def base(self) -> str:
        return 'master'

    @property
    def title(self) -> str:
        return f'Close out #{self.target.feature.number}'

    @property
    def body(self) -> str:
        return f'Closes #{self.target.feature.number}'


@dataclass(frozen=True, slots=True)
class Receipt:
    publication: Publication
    pr: IssueRef


@dataclass(frozen=True, slots=True)
class Pull:
    pr: IssueRef
    base: str
    head: str
    commit: Commit
    title: str
    body: str
    closes: tuple[IssueRef, ...]
    state: PullRequestState


@dataclass(frozen=True, slots=True)
class PublicationState:
    base_sha: str
    issue_state: IssueState
    ref: Commit | None
    pulls: tuple[Pull, ...]


@dataclass(frozen=True, slots=True)
class HostedCheck:
    pr: IssueRef
    commit: Commit
    producer: str  # authenticated workflow identity/revision, not the displayed check name
    run_id: str
    status: str


@dataclass(frozen=True, slots=True)
class Squash:
    commit: Commit  # squash SHA, verified single parent and exact delta on master
    message: str


@dataclass(frozen=True, slots=True)
class MergeState:
    pull: Pull
    base_sha: str  # current master, not the parent recorded in the PR's commit
    feature: FeatureEvidence
    check: HostedCheck | None
    closed_by: IssueRef | None = None  # actual issue closure event and its PR
    squash: Squash | None = None


@dataclass(frozen=True, slots=True)
class SquashCommand:
    receipt: Receipt
    expected: MergeState
    message: str


class Transport(Protocol):
    """Trusted bounded adapter. Never retry internally or echo requested facts.

    Verified absence is distinct from unknown (raise EvidenceReadError). Bind
    every observation to the actual same-repository identities. Publication
    reads cover the full branch-pair PR inventory, including closed PRs. Merge
    reads by PR and feature number, independent of the topic ref, and verify
    actual closing links, squash association, commit and issue closure event.
    Authenticate hosted results from protected policy, not candidate output.
    """

    def feature_evidence(self, target: Target) -> FeatureEvidence: ...

    def prepare(self, target: Target, sealed: SealedCloseout) -> Commit:
        """Prepare the same exact local commit on recovery; no remote side effects.

        Use fixed trusted commit metadata and exactly these regular-file
        additions, with the expected base as sole parent. Verify actual Git
        objects and do not read a checkout, session, or staging paths.
        """
        ...

    def publication_state(self, target: Target) -> PublicationState: ...

    def push(self, publication: Publication, sealed: SealedCloseout) -> None:
        """Push only the prepared sealed commit to a new topic ref, using an absence lease."""
        ...

    def open_pr(self, publication: Publication) -> None:
        """Request this exact same-repo head and master base once; never close an issue directly."""
        ...

    def merge_state(self, receipt: Receipt) -> MergeState: ...

    def conditional_squash(self, command: SquashCommand) -> str | bool | None:
        """Enforce all expected facts at the write boundary, not just head or a reread.

        False is definite rejection and cannot apply later. Otherwise return
        the reported squash SHA or None for ambiguity; always reconcile both
        merge and closure by reading them independently. No bypass or retry.
        """
        ...


class Outcome(StrEnum):
    BLOCKED = 'blocked'
    PUBLISHED = 'published'
    VERIFIED = 'verified'
    CHANGED_AT_WRITE = 'changed-at-write'
    DISCREPANCY = 'discrepancy'
    IN_FLIGHT = 'in-flight'
    BUSY = 'busy'
    UNKNOWN = 'unknown'


@dataclass(frozen=True, slots=True)
class Result:
    outcome: Outcome
    receipt: Receipt | None = None
    squash_sha: str | None = None
    hold: ReleaseStatus | None = None


class _Phase(IntEnum):
    PUSH_CLAIMED = 1
    PR_CLAIMED = 2
    PUBLISHED = 3


def _key(target: Target) -> tuple[str, int]:
    if not isinstance(target, Target) or not valid_sha(target.revision) or not valid_sha(target.base_sha):
        raise ValueError('expected an exact closeout target')
    _feature_tag(target.feature, target.slug)
    return target.feature.repo.casefold(), target.feature.number


def _matches_pull(pull: object, publication: Publication, state: PullRequestState) -> bool:
    return (isinstance(pull, Pull) and valid_issue_ref(pull.pr)
            and pull.pr.repo == publication.target.feature.repo and pull.pr != publication.target.feature
            and (pull.base, pull.head, pull.commit, pull.title, pull.body, pull.closes, pull.state) == (
                publication.base, publication.target.branch, publication.commit, publication.title,
                publication.body, (publication.target.feature,), state)
            and pull.state is state)


def _scope(target: Target, policy: Policy, evidence: FeatureEvidence) -> tuple[dict, str] | None:
    if not isinstance(evidence, FeatureEvidence):
        return None
    decision = decide_feature(target.feature, policy, evidence)
    if not decision.closeout_ready or decision.approval is None or decision.approval.sha != target.revision:
        return None
    projection = public_scope(target.feature, decision)
    fingerprint = request_fingerprint([
        projection, [asdict(plan) for plan in evidence.backlogs],
        [(story.issue, evidence.children[story.issue].prerequisites) for story in decision.completed_stories],
    ])
    return projection, fingerprint


def _revision_held(conn: sqlite3.Connection, target: Target) -> bool:
    return conn.execute('SELECT 1 FROM redesign_holds WHERE repo = ? AND feature = ? AND revision = ?',
                        (*_key(target), target.revision)).fetchone() is not None


@dataclass(frozen=True, slots=True)
class _PublicationRecord:
    fingerprint: str
    scope: str
    phase: _Phase
    pr: int | None


def _publication_row(conn: sqlite3.Connection, key: tuple[str, int]) -> _PublicationRecord | None:
    # Additive upgrade for existing #46/#49 journals, under the same writer lock.
    conn.execute('''CREATE TABLE IF NOT EXISTS closeout_publications (
        repo TEXT NOT NULL, feature INTEGER NOT NULL, fingerprint TEXT NOT NULL, scope TEXT NOT NULL,
        phase INTEGER NOT NULL, pr INTEGER, PRIMARY KEY (repo, feature),
        CHECK ((phase IN (1, 2) AND pr IS NULL) OR (phase = 3 AND pr IS NOT NULL AND pr > 0))
    ) STRICT''')
    row = conn.execute('SELECT fingerprint, scope, phase, pr FROM closeout_publications WHERE repo = ? AND feature = ?',
                       key).fetchone()
    if row is None:
        return None
    fingerprint, scope, phase, pr = row
    return _PublicationRecord(fingerprint, scope, _Phase(phase), pr)


class Publisher:
    """Run in the protected supervisor. The permit is an in-process capability, not a credential."""

    def __init__(self, directory: Path, policy: Policy, producer: str, permit: object, transport: Transport) -> None:
        if permit is None or not isinstance(producer, str) or not producer.strip():
            raise ValueError('trusted publication permit and hosted producer required')
        self._directory = directory.absolute()
        self._policy = policy
        self._producer = producer
        self._permit = permit
        self._transport = transport

    def publish(self, permit: object, target: Target, candidate: FrozenCloseout,
                root: Path, scanner: Path) -> Result:
        """Scan privately before any remote write. Only the scanned callback can send."""
        if permit is not self._permit:
            return Result(Outcome.BLOCKED)
        key = _key(target)
        result = Result(Outcome.UNKNOWN)

        def publish_sealed(sealed: SealedCloseout) -> None:
            nonlocal result
            with locked_writer(self._directory) as conn:
                result = self._publish_sealed(conn, key, target, sealed)

        try:
            status = release_closeout(candidate, root, target.feature, target.slug, scanner, publish_sealed)
            return result if status is ReleaseStatus.RELEASED else Result(Outcome.BLOCKED, hold=status)
        except WriterBusy:
            return Result(Outcome.BUSY)
        except (EvidenceReadError, OSError, sqlite3.Error):
            return Result(Outcome.UNKNOWN)

    def _publish_sealed(self, conn: sqlite3.Connection, key: tuple[str, int], target: Target,
                        sealed: SealedCloseout) -> Result:
        if _unsettled(conn, *key) or _revision_held(conn, target):
            return Result(Outcome.BLOCKED)
        scope = _scope(target, self._policy, self._transport.feature_evidence(target))
        if scope is None:
            return Result(Outcome.BLOCKED)
        projection, snapshot = scope
        # Already schema-checked; compare the vetted index to current trusted evidence.
        index = json.loads(sealed.files[0].data)
        if any(index[name] != expected for name, expected in projection.items()):
            return Result(Outcome.BLOCKED)
        commit = self._transport.prepare(target, sealed)
        if (not isinstance(commit, Commit) or commit.base_sha != target.base_sha or not valid_sha(commit.head_sha)
                or commit.head_sha == commit.base_sha or commit.delta_sha256 != delta_digest(sealed)):
            return Result(Outcome.BLOCKED)
        publication = Publication(target, commit)
        fingerprint = request_fingerprint(asdict(publication))
        row = _publication_row(conn, key)
        if row is not None and (row.fingerprint, row.scope) != (fingerprint, snapshot):
            return Result(Outcome.DISCREPANCY)
        phase = row.phase if row is not None else None
        state = self._publication_state(publication)
        if isinstance(state, Result):
            return state
        if phase is None:
            if state.ref is not None or state.pulls:
                return Result(Outcome.DISCREPANCY)
            conn.execute('INSERT INTO closeout_publications VALUES (?, ?, ?, ?, ?, NULL)',
                         (*key, fingerprint, snapshot, _Phase.PUSH_CLAIMED))
            phase = _Phase.PUSH_CLAIMED
            try:
                self._transport.push(publication, sealed)
            except (EvidenceReadError, OSError):
                pass
            state = self._publication_state(publication)
            if isinstance(state, Result):
                return state
        if state.ref is None:
            return Result(Outcome.IN_FLIGHT if phase == _Phase.PUSH_CLAIMED and not state.pulls
                          else Outcome.DISCREPANCY)
        if phase == _Phase.PUSH_CLAIMED:
            if state.pulls:
                return Result(Outcome.DISCREPANCY)
            if scope != _scope(target, self._policy, self._transport.feature_evidence(target)):
                return Result(Outcome.BLOCKED)
            conn.execute('UPDATE closeout_publications SET phase = ? WHERE repo = ? AND feature = ?',
                         (_Phase.PR_CLAIMED, *key))
            phase = _Phase.PR_CLAIMED
            try:
                self._transport.open_pr(publication)
            except (EvidenceReadError, OSError):
                pass
            state = self._publication_state(publication)
            if isinstance(state, Result):
                return state
        if state.ref != commit:
            return Result(Outcome.DISCREPANCY)
        if not state.pulls:
            return Result(Outcome.IN_FLIGHT if phase == _Phase.PR_CLAIMED else Outcome.DISCREPANCY)
        if len(state.pulls) != 1 or not _matches_pull(state.pulls[0], publication, PullRequestState.OPEN):
            return Result(Outcome.DISCREPANCY)
        found = state.pulls[0].pr
        if row is not None and row.phase is _Phase.PUBLISHED and found.number != row.pr:
            return Result(Outcome.DISCREPANCY)
        conn.execute('UPDATE closeout_publications SET phase = ?, pr = ? WHERE repo = ? AND feature = ?',
                     (_Phase.PUBLISHED, found.number, *key))
        return Result(Outcome.PUBLISHED, Receipt(publication, found))

    def _publication_state(self, publication: Publication) -> PublicationState | Result:
        state = self._transport.publication_state(publication.target)
        if (not isinstance(state, PublicationState) or not isinstance(state.issue_state, IssueState)
                or not valid_sha(state.base_sha) or not isinstance(state.pulls, tuple)):
            return Result(Outcome.UNKNOWN)
        if (state.issue_state is not IssueState.OPEN or state.base_sha != publication.target.base_sha
                or (state.ref is not None and state.ref != publication.commit)):
            return Result(Outcome.DISCREPANCY)
        return state

    def merge(self, receipt: Receipt) -> Result:
        """Accept only the journaled exact PR; verify squash and linked issue closure."""
        if (not isinstance(receipt, Receipt) or not isinstance(receipt.publication, Publication)
                or not isinstance(receipt.publication.commit, Commit) or not valid_issue_ref(receipt.pr)):
            raise ValueError('expected a closeout publication receipt')
        target = receipt.publication.target
        key = _key(target)
        if receipt.pr.repo != target.feature.repo or receipt.pr == target.feature:
            return Result(Outcome.BLOCKED)
        try:
            with locked_writer(self._directory) as conn:
                return self._merge(conn, key, receipt)
        except WriterBusy:
            return Result(Outcome.BUSY)
        except (EvidenceReadError, OSError, sqlite3.Error):
            return Result(Outcome.UNKNOWN)

    def _merge(self, conn: sqlite3.Connection, key: tuple[str, int], receipt: Receipt) -> Result:
        publication = receipt.publication
        fingerprint = request_fingerprint(asdict(publication))
        row = _publication_row(conn, key)
        if row is None or (row.fingerprint, row.phase, row.pr) != (fingerprint, _Phase.PUBLISHED, receipt.pr.number):
            return Result(Outcome.BLOCKED)
        # Share squash claims with stories so a redesign cannot pass a closeout
        # with unknown effects (and vice versa), even after a restart.
        pr_key = (key[0], receipt.pr.number)
        claim = conn.execute('SELECT fingerprint, message, observed_sha, verified FROM story_squashes '
                             'WHERE repo = ? AND pr = ?', pr_key).fetchone()
        if claim is not None:
            stored, message, sha, verified = claim
            if stored != fingerprint:
                return Result(Outcome.DISCREPANCY)
            if verified:
                return Result(Outcome.VERIFIED, squash_sha=sha)
            return self._verify(conn, pr_key, receipt, message, sha)
        if _unsettled(conn, *key) or _revision_held(conn, publication.target):
            return Result(Outcome.BLOCKED)
        state = self._transport.merge_state(receipt)
        if not isinstance(state, MergeState) or not isinstance(state.feature, FeatureEvidence):
            return Result(Outcome.UNKNOWN)
        if (not _matches_pull(state.pull, publication, PullRequestState.OPEN) or state.pull.pr != receipt.pr
                or state.feature.state is not IssueState.OPEN or state.closed_by is not None or state.squash is not None):
            return Result(Outcome.DISCREPANCY)
        scope = _scope(publication.target, self._policy, state.feature)
        check = state.check
        if (state.base_sha != publication.target.base_sha or scope is None or scope[1] != row.scope
                or not isinstance(check, HostedCheck) or check.pr != receipt.pr or check.commit != publication.commit
                or check.producer != self._producer or check.status != 'success'
                or not isinstance(check.run_id, str) or not check.run_id.strip()):
            return Result(Outcome.BLOCKED)
        message = f'Close out feature #{key[1]}\n\nRefs: #{key[1]}'
        send = getattr(self._transport, 'conditional_squash', None)
        if not callable(send):
            return Result(Outcome.BLOCKED)
        conn.execute('INSERT INTO story_squashes (repo, pr, feature, fingerprint, message) VALUES (?, ?, ?, ?, ?)',
                     (*pr_key, key[1], fingerprint, message))
        try:
            reported = send(SquashCommand(receipt, state, message))
        except (EvidenceReadError, OSError):
            reported = None
        if reported is False:
            conn.execute('DELETE FROM story_squashes WHERE repo = ? AND pr = ?', pr_key)
            return Result(Outcome.CHANGED_AT_WRITE)
        sha = reported if valid_sha(reported) else None
        if sha is not None:
            conn.execute('UPDATE story_squashes SET observed_sha = ? WHERE repo = ? AND pr = ?', (sha, *pr_key))
        return self._verify(conn, pr_key, receipt, message, sha)

    def _verify(self, conn: sqlite3.Connection, key: tuple[str, int], receipt: Receipt,
                message: str, expected_sha: str | None) -> Result:
        try:
            state = self._transport.merge_state(receipt)
        except (EvidenceReadError, OSError):
            return Result(Outcome.DISCREPANCY)
        publication = receipt.publication
        if (not isinstance(state, MergeState) or not _matches_pull(state.pull, publication, PullRequestState.MERGED)
                or state.pull.pr != receipt.pr or not isinstance(state.feature, FeatureEvidence)
                or not isinstance(state.squash, Squash) or not isinstance(state.squash.commit, Commit)):
            return Result(Outcome.DISCREPANCY)
        commit = state.squash.commit
        if (not valid_sha(commit.head_sha) or commit.head_sha in (publication.commit.head_sha, commit.base_sha)
                or (expected_sha is not None and commit.head_sha != expected_sha)
                or (commit.base_sha, commit.delta_sha256) != (publication.commit.base_sha, publication.commit.delta_sha256)
                or not isinstance(state.squash.message, str) or state.squash.message.rstrip('\n') != message):
            return Result(Outcome.DISCREPANCY)
        closed = state.feature.state is IssueState.CLOSED and state.closed_by == receipt.pr
        conn.execute('UPDATE story_squashes SET observed_sha = ?, verified = ? WHERE repo = ? AND pr = ?',
                     (commit.head_sha, int(closed), *key))
        return Result(Outcome.VERIFIED, squash_sha=commit.head_sha) if closed else Result(Outcome.DISCREPANCY)
