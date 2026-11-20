"""Story merge broker (fixture transport only)."""

import sqlite3
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .checked_e2e import Binding, Check, Evidence, check_passed, review_passed, valid_binding
from .feature_tree import EvidenceReadError, IssueRef, valid_issue_ref
from .github_writes import WriterBusy, _digest, _locked_writer
from .merge import Candidate, CandidateReader, ChangedPath, ClassifiedStory, ClosingIssue, DesignProtection, classify
from .readiness import (
    Blocker, FeatureEvidence, IssueState, Policy, PullRequestState, Reason, RevisionRequest, decide_feature, valid_sha,
)


@dataclass(frozen=True)
class StoryRequest:
    feature: IssueRef
    story: IssueRef
    binding: Binding


@dataclass(frozen=True)
class MergePolicy:
    readiness: Policy
    design_issues: frozenset[IssueRef]
    ci_producer: str
    review_producer: str
    e2e_producer: str


@dataclass(frozen=True)
class E2EResult:
    check: Check
    ci_run: str
    review_run: str
    cleanup_verified: bool


@dataclass(frozen=True)
class GateEvidence:
    feature: IssueRef
    feature_evidence: FeatureEvidence
    checks: Evidence
    review_clear: bool | None  # current candidate has no outstanding review blocks
    e2e: E2EResult | None


@dataclass(frozen=True)
class SquashCommit:
    head_sha: str
    base: str
    parents: tuple[str, ...]
    sha: str
    message: str


@dataclass(frozen=True)
class StoryState:
    pr: IssueRef
    story: IssueRef
    pr_state: PullRequestState
    issue_state: IssueState
    closed_by: IssueRef | None = None
    commit: SquashCommit | None = None


@dataclass(frozen=True)
class SquashCommand:
    request: StoryRequest
    candidate: Candidate  # includes the server-side expected head and base
    paths: frozenset[ChangedPath]
    links: frozenset[ClosingIssue]
    gates: GateEvidence
    expected_state: StoryState
    message: str


class StoryReader(CandidateReader, Protocol):
    """Trusted, bounded reads; unverifiable or incomplete evidence raises EvidenceReadError."""

    def gates(self, request: StoryRequest) -> GateEvidence:
        """Authenticate producers, candidate-bound review state and current inventories."""
        ...

    def story_state(self, pr: IssueRef, story: IssueRef) -> StoryState:
        """Read by PR and issue number, even after branch deletion.

        For a merged PR, resolve its actual commit on the target branch and
        independently read the commit parents and full message. Use the actual
        issue closure event and its PR, not closing intent or a closed flag
        alone. Missing/ambiguous reads must not invent a commit or closure.
        """
        ...


class ConditionalTransport(StoryReader, Protocol):
    def conditional_squash(self, command: SquashCommand) -> bool:
        """Squash once only if expected facts still hold at the write boundary.

        Enforce the combination of repository, PR, target ref and base SHA,
        head SHA, design metadata, gate evidence and open story/PR state at the
        write, not by a client-side read before a head-only merge. Do not retry
        or bypass protection. False means definitely rejected and cannot apply
        later; True or an I/O failure still requires reconciliation.
        """
        ...


class Outcome(StrEnum):
    VERIFIED = "verified"
    NO_CONDITIONAL_TRANSPORT = "no-conditional-transport"
    BLOCKED = "blocked"
    CHANGED_AT_WRITE = "changed-at-write"
    DISCREPANCY = "discrepancy"
    REGISTERED = "registered"
    BUSY = "busy"
    IN_FLIGHT = "in-flight"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    squash_sha: str | None = None


def _gate_passes(request: StoryRequest, policy: MergePolicy, gates: GateEvidence) -> bool:
    if (not isinstance(gates, GateEvidence) or gates.feature != request.feature
            or not isinstance(gates.feature_evidence, FeatureEvidence) or gates.review_clear is not True):
        return False
    scope = decide_feature(request.feature, policy.readiness, gates.feature_evidence)
    binding = request.binding
    if (scope.approval is None or scope.approval.sha != binding.adr_revision
            or binding.pr.number not in scope.merge_candidates
            or Blocker(request.story.number, Reason.EXISTING_PR, (binding.pr.number,)) not in scope.blockers):
        return False
    checks = gates.checks
    if (not isinstance(checks, Evidence) or checks.binding != binding
            or checks.open_nondraft is not True or checks.redesign_pending is not False
            or not check_passed(checks.ci, binding, policy.ci_producer)
            or not review_passed(checks.review, binding, policy.review_producer)):
        return False
    e2e = gates.e2e
    return (isinstance(e2e, E2EResult) and check_passed(e2e.check, binding, policy.e2e_producer)
            and e2e.cleanup_verified is True and e2e.ci_run == checks.ci.run_id
            and e2e.review_run == checks.review.check.run_id)


def _valid_target(feature: IssueRef, story: IssueRef) -> bool:
    return (valid_issue_ref(feature) and valid_issue_ref(story) and feature.repo == story.repo
            and feature != story)


def _unsettled(conn: sqlite3.Connection, repo: str, feature: int) -> bool:
    return conn.execute("SELECT 1 FROM story_squashes WHERE repo = ? AND feature = ? AND verified = 0 LIMIT 1",
                        (repo, feature)).fetchone() is not None


class MergeBroker:
    """One protected writer journal per repository. No production transport is provided."""

    def __init__(self, directory: Path, policy: MergePolicy, transport: StoryReader) -> None:
        producers = (policy.ci_producer, policy.review_producer, policy.e2e_producer)
        if not all(isinstance(p, str) and p.strip() for p in producers) or len(set(producers)) != len(producers):
            raise ValueError("distinct trusted CI, review and E2E producers are required")
        self._directory = directory.absolute()
        self._policy = policy
        self._design = DesignProtection(policy.readiness.adr_path, policy.design_issues)
        self._transport = transport

    def reconcile(self, request: StoryRequest) -> Result:
        """Admit at most one unsettled squash; recover without needing the topic branch."""
        if (not isinstance(request, StoryRequest) or not _valid_target(request.feature, request.story)
                or not valid_binding(request.binding) or request.binding.pr.repo != request.feature.repo
                or request.binding.pr in (request.feature, request.story)):
            raise ValueError("a feature, its story and an exact story PR binding are required")
        try:
            with _locked_writer(self._directory) as conn:
                return self._reconcile(conn, request)
        except WriterBusy:
            return Result(Outcome.BUSY)
        except (EvidenceReadError, OSError, sqlite3.Error):
            return Result(Outcome.UNKNOWN)

    def register_redesign(self, request: RevisionRequest) -> Outcome:
        """Persist a hold before the loop registers a redesign externally.

        A caller may publish the request/link its design child only after this
        returns REGISTERED. The hold stays for that revision even if publication
        fails; only a verified successor approval can make it irrelevant.
        """
        if (not isinstance(request, RevisionRequest) or not _valid_target(request.feature, request.issue)
                or not valid_sha(request.revision) or type(request.attempt) is not int or request.attempt < 1):
            raise ValueError("a revision-bound Builder contradiction is required")
        repo = request.feature.repo.casefold()
        try:
            with _locked_writer(self._directory) as conn:
                if _unsettled(conn, repo, request.feature.number):
                    return Outcome.IN_FLIGHT
                conn.execute("INSERT OR IGNORE INTO redesign_holds VALUES (?, ?, ?)",
                             (repo, request.feature.number, request.revision))
                return Outcome.REGISTERED
        except WriterBusy:
            return Outcome.BUSY
        except (OSError, sqlite3.Error):
            return Outcome.UNKNOWN

    def _checked_command(self, request: StoryRequest, state: StoryState) -> SquashCommand | None:
        binding = request.binding
        story = classify(binding.pr, self._design, self._transport)
        if not isinstance(story, ClassifiedStory):
            return None
        candidate = story.candidate
        if ((candidate.base, candidate.base_sha, candidate.head_sha) != ("master", binding.base_sha, binding.head_sha)
                or {link.issue for link in story.links} != {request.story}):
            return None
        gates = self._transport.gates(request)
        if not _gate_passes(request, self._policy, gates):
            return None
        # Don't copy candidate-controlled closing keywords into the commit.
        message = (f"Complete story #{request.story.number}\n\n"
                   f"Refs: #{request.feature.number}\nRefs: #{request.story.number}")
        return SquashCommand(request, candidate, story.paths, story.links, gates, state, message)

    def _reconcile(self, conn: sqlite3.Connection, request: StoryRequest) -> Result:
        pr = request.binding.pr
        feature = request.feature.number
        key = (pr.repo.casefold(), pr.number)
        fingerprint = _digest(asdict(request))
        row = conn.execute("""SELECT fingerprint, message, observed_sha, verified FROM story_squashes
                              WHERE repo = ? AND pr = ?""", key).fetchone()
        if row is not None:
            stored, message, observed_sha, verified = row
            if stored != fingerprint:
                return Result(Outcome.DISCREPANCY)
            if verified:
                return Result(Outcome.VERIFIED, observed_sha)
            return self._verify(conn, request, key, message, observed_sha)

        if _unsettled(conn, key[0], feature):
            return Result(Outcome.IN_FLIGHT)
        if conn.execute("SELECT 1 FROM redesign_holds WHERE repo = ? AND feature = ? AND revision = ?",
                        (key[0], feature, request.binding.adr_revision)).fetchone():
            return Result(Outcome.BLOCKED)
        state = self._transport.story_state(pr, request.story)
        if (not isinstance(state, StoryState) or not isinstance(state.pr_state, PullRequestState)
                or not isinstance(state.issue_state, IssueState)):
            return Result(Outcome.UNKNOWN)
        if state != StoryState(pr, request.story, PullRequestState.OPEN, IssueState.OPEN):
            return Result(Outcome.DISCREPANCY)
        command = self._checked_command(request, state)
        if command is None:
            return Result(Outcome.BLOCKED)
        send = getattr(self._transport, "conditional_squash", None)
        if not callable(send):
            return Result(Outcome.NO_CONDITIONAL_TRANSPORT)
        if command != self._checked_command(request, state):
            return Result(Outcome.BLOCKED)

        # Claim before contacting the transport. If interrupted after this commit,
        # even unchanged external state is not proof the request cannot apply.
        conn.execute("""INSERT INTO story_squashes (repo, pr, feature, fingerprint, message)
                        VALUES (?, ?, ?, ?, ?)""", (*key, feature, fingerprint, command.message))
        try:
            applied = send(command)
        except (EvidenceReadError, OSError):
            applied = None
        if applied is False:
            conn.execute("DELETE FROM story_squashes WHERE repo = ? AND pr = ?", key)
            return Result(Outcome.CHANGED_AT_WRITE)
        return self._verify(conn, request, key, command.message, None)

    def _verify(self, conn: sqlite3.Connection, request: StoryRequest, key: tuple[str, int],
                message: str, observed_sha: str | None) -> Result:
        binding = request.binding
        try:
            state = self._transport.story_state(binding.pr, request.story)
        except (EvidenceReadError, OSError):
            return Result(Outcome.IN_FLIGHT)
        if not isinstance(state, StoryState):
            return Result(Outcome.IN_FLIGHT)
        if (not isinstance(state.pr_state, PullRequestState) or not isinstance(state.issue_state, IssueState)):
            return Result(Outcome.IN_FLIGHT)
        if (observed_sha is None
                and state == StoryState(binding.pr, request.story, PullRequestState.OPEN, IssueState.OPEN)):
            return Result(Outcome.IN_FLIGHT)
        commit = state.commit
        if ((state.pr, state.story) != (binding.pr, request.story) or state.pr_state is not PullRequestState.MERGED
                or not isinstance(commit, SquashCommit) or not valid_sha(commit.sha)
                or commit.sha in (binding.head_sha, binding.base_sha, binding.merge_sha)
                or (observed_sha is not None and commit.sha != observed_sha)
                or (commit.head_sha, commit.base, commit.parents) != (binding.head_sha, "master", (binding.base_sha,))
                or not isinstance(commit.message, str) or commit.message.rstrip("\n") != message):
            return Result(Outcome.DISCREPANCY)
        closed = state.issue_state is IssueState.CLOSED and state.closed_by == binding.pr
        conn.execute("UPDATE story_squashes SET observed_sha = ?, verified = ? WHERE repo = ? AND pr = ?",
                     (commit.sha, int(closed), *key))
        return Result(Outcome.VERIFIED, commit.sha) if closed else Result(Outcome.DISCREPANCY)
