import multiprocessing
import os
from dataclasses import replace

import pytest

from tools.agentic_dev_loop.checked_e2e import Binding, Check, Evidence, FixtureAttestation, Review
from tools.agentic_dev_loop.feature_tree import EvidenceReadError, FeatureTree, IssueRef, Phase
from tools.agentic_dev_loop.github_writes import initialize_writer
from tools.agentic_dev_loop.merge import Candidate, ChangedPath, ChangeKind, ClosingIssue, Page
from tools.agentic_dev_loop.readiness import (
    AdrAtMerge, Backlog, DesignEvidence, FeatureEvidence, IssueState, Policy,
    PullRequest, PullRequestState, RevisionRequest, StoryEvidence,
)
from tools.agentic_dev_loop.story_merge import (
    E2EResult, GateEvidence, MergeBroker, MergePolicy, Outcome, SquashCommit, StoryRequest, StoryState,
)


REPO = "fixture/floe"
FEATURE, DESIGN, STORY, PR = (IssueRef(REPO, number) for number in (29, 30, 42, 99))
ADR = ".agents/engineering/29-agentic-dev-loop/design.md"
BASE, HEAD, TESTED, REVISION, SQUASH, OTHER = (c * 40 for c in "abcdef")
BINDING = Binding(PR, HEAD, BASE, TESTED, REVISION)
REQUEST = StoryRequest(FEATURE, STORY, BINDING)
CANDIDATE = Candidate(PR, "Add fixture behavior", "maintainer", "master", BASE, HEAD)
POLICY = MergePolicy(Policy("maintainer", ADR), frozenset({DESIGN}), "hosted-ci", "pr-agent", "e2e-supervisor")
REVISION_REQUEST = RevisionRequest(FEATURE, STORY, REVISION, 1)
MESSAGE = "Complete story #42\n\nRefs: #29\nRefs: #42"
DESIGN_PR = PullRequest(55, REPO, "master", (DESIGN,), PullRequestState.MERGED,
                        REVISION, True, "maintainer")
STORY_PR = PullRequest(PR.number, REPO, "master", (STORY,), PullRequestState.OPEN,
                       approved_revision=REVISION)
STORY_EVIDENCE = StoryEvidence(IssueState.OPEN, False, REVISION, (), PR.number, (STORY_PR,))
FEATURE_EVIDENCE = FeatureEvidence(
    FeatureTree({DESIGN.number: Phase.DESIGN, STORY.number: Phase.STORY}),
    {DESIGN.number: DesignEvidence(IssueState.CLOSED, False, None, (DESIGN_PR,),
                                   AdrAtMerge(ADR, REVISION, "100644", True)),
     STORY.number: STORY_EVIDENCE},
    IssueState.OPEN, False, False, (Backlog(REVISION, (STORY.number,)),), revision_requests=(),
)
CI = Check(BINDING, POLICY.ci_producer, "ci-run", "success")
REVIEW = Review(Check(BINDING, POLICY.review_producer, "review-run", "success"), ())
CHECKS = Evidence(BINDING, True, False, CI, REVIEW)
E2E = E2EResult(Check(BINDING, POLICY.e2e_producer, "e2e-run", "success"), CI.run_id, REVIEW.check.run_id, True)
GATES = GateEvidence(FEATURE, FEATURE_EVIDENCE, CHECKS, True, E2E)


def successor():
    issue = IssueRef(REPO, 31)
    merged = replace(DESIGN_PR, number=56, closes=(issue,), squash_sha=OTHER, approved_revision=REVISION)
    return replace(FEATURE_EVIDENCE,
                   tree=FeatureTree({**FEATURE_EVIDENCE.tree.phases, issue.number: Phase.DESIGN}),
                   children={**FEATURE_EVIDENCE.children,
                             issue.number: DesignEvidence(IssueState.CLOSED, False, DESIGN.number, (merged,),
                                                          AdrAtMerge(ADR, OTHER, "100644", True)),
                             STORY.number: replace(STORY_EVIDENCE, revision=OTHER,
                                                   candidates=(replace(STORY_PR, approved_revision=OTHER),))},
                   backlogs=(*FEATURE_EVIDENCE.backlogs, Backlog(OTHER, (STORY.number,))))


OPEN = StoryState(PR, STORY, PullRequestState.OPEN, IssueState.OPEN)
COMMIT = SquashCommit(HEAD, "master", (BASE,), SQUASH, MESSAGE)
DONE = StoryState(PR, STORY, PullRequestState.MERGED, IssueState.CLOSED, PR, COMMIT)


class HeadOnly:
    def __init__(self):
        self.current = CANDIDATE
        self.paths = (ChangedPath("tools/new.py"),)
        self.links = (ClosingIssue(STORY, "Story: Fixture"),)
        self.closing_total = None
        self.gates_now = GATES
        self.state = OPEN
        self.requests = []
        self.after_first_gates = None
        self.gate_reads = 0

    def candidate(self, pr):
        assert pr == PR and self.current is not None
        return self.current

    def changed_paths(self, candidate, cursor):
        assert cursor is None
        return Page(self.current, self.paths, len(self.paths), None)

    def closing_issues(self, candidate, cursor):
        assert cursor is None
        total = len(self.links) if self.closing_total is None else self.closing_total
        return Page(self.current, self.links, total, None)

    def gates(self, request):
        assert request == REQUEST
        self.gate_reads += 1
        current = self.gates_now
        if self.gate_reads == 1 and self.after_first_gates:
            self.after_first_gates()
        return current

    def story_state(self, pr, story):
        assert (pr, story) == (PR, STORY)
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    def squash(self, candidate):
        pytest.fail("a head-only transport must not be used")


class Atomic(HeadOnly):
    def __init__(self):
        super().__init__()
        self.before_write = lambda: None
        self.after_write = lambda: None
        self.writes = 0

    def conditional_squash(self, command):
        self.requests.append(command)
        self.before_write()
        if ((self.current, frozenset(self.paths), frozenset(self.links), self.gates_now, self.state)
                != (command.candidate, command.paths, command.links, command.gates, command.expected_state)):
            return False
        self.writes += 1
        self.state = replace(DONE, commit=replace(COMMIT, message=command.message))
        self.after_write()
        return True


@pytest.fixture
def directory(tmp_path):
    directory = tmp_path / "journal"
    initialize_writer(directory)
    return directory


def broker(directory, transport):
    return MergeBroker(directory, POLICY, transport)


def test_fully_checked_squash_verifies_distinct_commit_and_closure(directory):
    transport = Atomic()
    transport.paths += (ChangedPath(ADR.replace('design.md', 'decisions.md')),)

    result = broker(directory, transport).reconcile(REQUEST)

    assert result.outcome is Outcome.VERIFIED and result.squash_sha == SQUASH
    [command] = transport.requests
    assert command.candidate.head_sha == HEAD and command.candidate.base_sha == BASE
    assert command.message == MESSAGE
    assert transport.writes == 1


def test_squash_message_does_not_copy_closing_keywords_from_title(directory):
    transport = Atomic()
    transport.current = replace(CANDIDATE, title="Add behavior; fixes #29")
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.VERIFIED
    assert [command.message for command in transport.requests] == [MESSAGE]


def test_head_precondition_is_not_an_atomic_base_precondition(directory):
    transport = HeadOnly()
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.NO_CONDITIONAL_TRANSPORT
    assert transport.requests == []


@pytest.mark.parametrize('bad_gates', [
    replace(GATES, checks=replace(CHECKS, binding=replace(BINDING, head_sha=OTHER))),
    replace(GATES, checks=replace(CHECKS, binding=replace(BINDING, base_sha=OTHER))),
    replace(GATES, checks=replace(CHECKS, open_nondraft=False)),
    replace(GATES, checks=replace(CHECKS, redesign_pending=True)),
    replace(GATES, checks=replace(CHECKS, ci=replace(CI, producer="impostor"))),
    replace(GATES, checks=replace(CHECKS, ci=replace(CI, status="pending"))),
    replace(GATES, checks=replace(CHECKS, ci=replace(CI, binding=replace(BINDING, merge_sha=OTHER)))),
    replace(GATES, checks=replace(CHECKS, review=None)),
    replace(GATES, checks=replace(CHECKS, review=replace(REVIEW, check=replace(REVIEW.check, producer="impostor")))),
    replace(GATES, checks=replace(CHECKS, review=replace(REVIEW, check=replace(REVIEW.check, status="failure")))),
    replace(GATES, checks=replace(CHECKS, review=replace(REVIEW, findings=None))),
    replace(GATES, checks=replace(CHECKS, review=replace(REVIEW, findings=("fixed",)))),
    replace(GATES, review_clear=False),
    replace(GATES, e2e=replace(E2E, check=replace(E2E.check, status="failure"))),
    replace(GATES, e2e=replace(E2E, check=replace(E2E.check, producer="impostor"))),
    replace(GATES, e2e=replace(E2E, cleanup_verified=False)),
    replace(GATES, e2e=replace(E2E, ci_run="previous-run")),
    replace(GATES, e2e=replace(E2E, review_run="previous-run")),
    replace(GATES, e2e=replace(E2E, check=replace(E2E.check, binding=replace(BINDING, base_sha=OTHER)))),
    replace(GATES, e2e=FixtureAttestation(BINDING, CI.run_id, REVIEW.check.run_id)),
    replace(GATES, feature_evidence=successor()),
    replace(GATES, feature_evidence=replace(FEATURE_EVIDENCE, revision_requests=(REVISION_REQUEST,))),
    replace(GATES, feature_evidence=replace(FEATURE_EVIDENCE, backlogs=(Backlog(REVISION, (), (STORY.number,)),))),
    replace(GATES, feature_evidence=replace(FEATURE_EVIDENCE, children={
        **FEATURE_EVIDENCE.children, STORY.number: replace(STORY_EVIDENCE, revision=OTHER)})),
])
def test_each_gate_independently_blocks(directory, bad_gates):
    transport = Atomic()
    transport.gates_now = bad_gates
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.BLOCKED
    assert transport.requests == []


@pytest.mark.parametrize('field', ['head_sha', 'base_sha', 'title', 'revision', 'path', 'closing_link', 'truncated_links'])
def test_final_recheck_catches_change(directory, field):
    transport = Atomic()

    def change():
        if field == 'revision':
            transport.gates_now = replace(GATES, feature_evidence=successor())
        elif field == 'path':
            transport.paths += (ChangedPath(ADR, ChangeKind.DELETE),)
        elif field == 'closing_link':
            transport.links += (ClosingIssue(DESIGN, 'Retitled design'),)
        elif field == 'truncated_links':
            transport.closing_total = 2
        else:
            transport.current = replace(CANDIDATE, **{field: 'Design: Now design' if field == 'title' else OTHER})

    transport.after_first_gates = change
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.BLOCKED
    assert transport.requests == []


@pytest.mark.parametrize('field', ['base_sha', 'head_sha', 'title'])
def test_conditional_transport_rejects_changes_after_last_read(directory, field):
    transport = Atomic()
    value = 'Design: Now design' if field == 'title' else OTHER
    transport.before_write = lambda: setattr(transport, 'current', replace(CANDIDATE, **{field: value}))

    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.CHANGED_AT_WRITE
    assert len(transport.requests) == 1 and transport.writes == 0


def test_scope_for_a_different_story_cannot_authorize_the_same_pr_number(directory):
    transport = Atomic()
    other = IssueRef(REPO, 43)
    transport.gates_now = replace(GATES, feature_evidence=replace(
        FEATURE_EVIDENCE, tree=FeatureTree({**FEATURE_EVIDENCE.tree.phases, other.number: Phase.STORY}),
        children={**FEATURE_EVIDENCE.children, other.number: replace(
            STORY_EVIDENCE, candidates=(replace(STORY_PR, closes=(other,)),))},
        backlogs=(Backlog(REVISION, (other.number,), (STORY.number,)),),
    ))
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.BLOCKED
    assert transport.requests == []


def test_redesign_and_merge_share_the_writer_lock_and_redesign_leaves_a_hold(directory):
    transport = Atomic()
    concurrent = []

    def change():
        concurrent.append(broker(directory, transport).register_redesign(REVISION_REQUEST))
        transport.gates_now = replace(GATES, checks=replace(CHECKS, redesign_pending=True))

    transport.after_first_gates = change
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.BLOCKED
    assert concurrent == [Outcome.BUSY]
    assert broker(directory, transport).register_redesign(REVISION_REQUEST) is Outcome.REGISTERED
    transport.gates_now = GATES  # external evidence has not caught up with registration
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.BLOCKED
    assert transport.requests == []


def test_final_recheck_catches_premature_closure_without_sending(directory):
    transport = Atomic()
    transport.after_first_gates = lambda: setattr(transport, 'state', replace(OPEN, issue_state=IssueState.CLOSED))
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.DISCREPANCY
    assert transport.requests == []


def test_premature_close_never_squashes(directory):
    transport = Atomic()
    transport.state = replace(OPEN, issue_state=IssueState.CLOSED)
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.DISCREPANCY
    assert transport.requests == []


@pytest.mark.parametrize('bad_result', [
    replace(DONE, issue_state=IssueState.OPEN, closed_by=None),
    replace(DONE, closed_by=None),
    replace(DONE, closed_by=IssueRef(REPO, 98)),
    replace(DONE, pr=IssueRef(REPO, 98)),
    replace(DONE, commit=replace(COMMIT, message="wrong message")),
    replace(DONE, commit=replace(COMMIT, sha=TESTED)),
    replace(DONE, commit=replace(COMMIT, parents=(OTHER,))),
])
def test_transport_success_alone_does_not_establish_completion(directory, bad_result):
    transport = Atomic()
    transport.after_write = lambda: setattr(transport, 'state', bad_result)
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.DISCREPANCY
    assert transport.writes == 1


def test_lost_reply_and_branch_deletion_reconcile_by_pr_after_reopen(directory):
    transport = Atomic()

    def lose_reply():
        transport.current = None  # branch gone; live candidate endpoints are no longer usable
        transport.state = EvidenceReadError('temporarily unavailable')
        raise OSError('lost reply')

    transport.after_write = lose_reply
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.IN_FLIGHT
    assert broker(directory, transport).register_redesign(REVISION_REQUEST) is Outcome.IN_FLIGHT
    transport.state = DONE
    result = broker(directory, transport).reconcile(REQUEST)
    assert result.outcome is Outcome.VERIFIED and result.squash_sha == SQUASH
    assert len(transport.requests) == transport.writes == 1


def test_discrepancy_keeps_claim_and_blocks_redesign_until_completion(directory):
    transport = Atomic()
    transport.after_write = lambda: setattr(transport, 'state', replace(DONE, issue_state=IssueState.OPEN, closed_by=None))
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.DISCREPANCY
    assert broker(directory, transport).register_redesign(REVISION_REQUEST) is Outcome.IN_FLIGHT
    transport.state = DONE
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.VERIFIED
    assert broker(directory, transport).register_redesign(REVISION_REQUEST) is Outcome.REGISTERED
    assert len(transport.requests) == 1


def test_process_loss_at_send_boundary_leaves_claim_and_rejects_rebinding(directory):
    def interrupted():
        transport = Atomic()
        transport.before_write = lambda: os._exit(24)
        broker(directory, transport).reconcile(REQUEST)

    worker = multiprocessing.get_context('fork').Process(target=interrupted)
    worker.start()
    try:
        worker.join(timeout=5)
        assert worker.exitcode == 24
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=5)
        worker.close()

    transport = Atomic()
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.IN_FLIGHT
    changed = replace(REQUEST, binding=replace(BINDING, head_sha=OTHER))
    assert broker(directory, transport).reconcile(changed).outcome is Outcome.DISCREPANCY
    assert transport.requests == []


def test_unchanged_readback_after_lost_reply_does_not_allow_blind_retry(directory):
    transport = Atomic()

    def lose_reply():
        raise OSError('write may still be in flight')

    transport.before_write = lose_reply
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.IN_FLIGHT
    assert broker(directory, transport).reconcile(REQUEST).outcome is Outcome.IN_FLIGHT
    assert len(transport.requests) == 1 and transport.writes == 0
