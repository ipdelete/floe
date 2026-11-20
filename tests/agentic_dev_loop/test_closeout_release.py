from dataclasses import fields, replace

import pytest

from test_closeout import SLUG, TAG, freeze, inputs  # noqa: F401
from test_closeout_policy import candidate, fake_scanner, gate  # noqa: F401
from test_readiness import FEATURE, POLICY, SECOND
from tools.agentic_dev_loop import closeout_release as release
from tools.agentic_dev_loop.closeout_policy import delta_digest
from tools.agentic_dev_loop.feature_tree import IssueRef
from tools.agentic_dev_loop.github_writes import initialize_writer
from tools.agentic_dev_loop.hosted_closeout import Commit
from tools.agentic_dev_loop.readiness import Backlog, DesignEvidence, IssueState, PullRequestState, RevisionRequest
from tools.agentic_dev_loop.story_merge import MergeBroker, MergePolicy, Outcome as StoryOutcome

BASE, HEAD, SQUASH = 'e' * 40, 'a' * 40, 'c' * 40
PR = IssueRef(FEATURE.repo, 88)
OTHER = IssueRef(FEATURE.repo, 89)
SOURCE = 'fixture/protected-closeout@trusted-revision'


class Remote:
    def __init__(self, feature):
        self.feature = feature
        self.ref = None
        self.prs = ()
        self.calls = []
        self.lose = None
        self.land = True
        self.closure = None
        self.squash_commit = None
        self.check = None
        self.accepted_sha = SQUASH
        self.current_base = BASE

    def prepare(self, target, sealed):
        return Commit(target.base_sha, HEAD, delta_digest(sealed))

    def publication_state(self, target):
        return release.PublicationState(BASE, self.feature.state, self.ref, self.prs)

    def feature_evidence(self, target):
        return self.feature

    def push(self, publication, sealed):
        self.calls.append(('push', publication, sealed))
        if self.land:
            self.ref = publication.commit
        if self.lose == 'push':
            raise OSError('synthetic lost reply')

    def open_pr(self, publication):
        self.calls.append(('pr', publication))
        if self.land:
            self.landed_pr(publication)
        if self.lose == 'pr':
            raise OSError('synthetic lost reply')

    def landed_pr(self, publication):
        self.prs = (release.Pull(PR, publication.base, publication.target.branch, publication.commit,
                                 publication.title, publication.body, (FEATURE,), PullRequestState.OPEN),)

    def merge_state(self, receipt):
        return release.MergeState(self.prs[0], self.current_base, self.feature, self.check,
                                  self.closure, self.squash_commit)

    def conditional_squash(self, command):
        self.calls.append(('squash', command))
        if command.expected != self.merge_state(command.receipt):
            return False
        if self.land:
            self.prs = (replace(self.prs[0], state=PullRequestState.MERGED),)
            self.squash_commit = release.Squash(replace(command.receipt.publication.commit, head_sha=SQUASH),
                                                command.message)
            self.close_feature(PR)
        if self.lose == 'squash':
            raise OSError('synthetic lost reply')
        return self.accepted_sha

    def close_feature(self, closer):
        self.feature = replace(self.feature, state=IssueState.CLOSED)
        sha = self.squash_commit.commit.head_sha if self.squash_commit else BASE
        self.closure = release.Closure(closer, sha)


@pytest.fixture
def setup(tmp_path, inputs, fake_scanner, candidate):
    root, evidence, _, _ = inputs
    scanner, calls, _ = fake_scanner
    remote = Remote(evidence)
    target = release.Target(FEATURE, SLUG, SECOND, BASE)
    token = object()
    journal = tmp_path / 'journal'
    initialize_writer(journal)

    def reopen():
        return release.Publisher(journal, POLICY, SOURCE, token, remote)

    def publish(publisher, auth=token):
        calls.clear()  # reset scanner subprocess fixture's control
        return publisher.publish(auth, target, candidate, root, scanner)

    return reopen, publish, remote, target


@pytest.mark.parametrize('problem', ['no-authorization', 'bytes', 'path', 'hold'])
def test_held_or_unauthorized_candidate_never_pushes(setup, candidate, fake_scanner, problem):
    reopen, publish, remote, _ = setup
    if problem == 'bytes':
        path = candidate.directory / 'summary.md'
        path.chmod(0o600)
        path.write_bytes(b'Not the frozen summary.\n')
    elif problem == 'path':
        (candidate.directory / 'summary.md').rename(candidate.directory / 'renamed.md')
    elif problem == 'hold':
        fake_scanner[0].write_bytes(b'wrong scanner checksum')
    result = publish(reopen(), None) if problem == 'no-authorization' else publish(reopen())
    assert result.receipt is None and not remote.calls and remote.ref is None


@pytest.mark.parametrize('lose', [None, 'push', 'pr'])
def test_publish_exact_delta_and_feature_only_pr_even_on_lost_reply(setup, candidate, lose):
    reopen, publish, remote, target = setup
    remote.lose = lose
    result = publish(reopen())
    assert result.outcome is release.Outcome.PUBLISHED
    assert publish(reopen()) == result
    push, pr = remote.calls
    assert push[:2] == ('push', result.receipt.publication)
    assert pr == ('pr', result.receipt.publication)
    assert {str(f.path): f.data for f in push[2].files} == {
        f'.agents/engineering/{TAG}/index.json': candidate.index,
        f'.agents/engineering/{TAG}/summary.md': candidate.summary,
    }
    assert {f.name for f in fields(push[2])} == {'files'}
    assert remote.prs[0].base == 'master' and remote.prs[0].head == target.branch
    assert remote.prs[0].body == f'Closes #{FEATURE.number}'
    assert result.receipt.pr == PR


@pytest.mark.parametrize('lose', ['push', 'pr'])
def test_absent_or_conflicting_readback_after_lost_reply_never_replays(setup, lose):
    reopen, publish, remote, _ = setup
    if lose == 'push':
        remote.push = unlanded(remote, 'push')
    else:
        remote.open_pr = unlanded(remote, 'pr')
    assert publish(reopen()).outcome is release.Outcome.IN_FLIGHT
    assert publish(reopen()).outcome is release.Outcome.IN_FLIGHT
    assert [call[0] for call in remote.calls].count(lose) == 1
    publication = remote.calls[-1][1]
    if lose == 'push':
        remote.ref = replace(publication.commit, head_sha='1' * 40)
    else:
        remote.landed_pr(publication)
        remote.prs = (replace(remote.prs[0], body='Closes #5'),)
    assert publish(reopen()).outcome is release.Outcome.DISCREPANCY
    if lose == 'push':
        remote.ref = publication.commit
    else:
        remote.prs = (replace(remote.prs[0], body=publication.body),)
    assert publish(reopen()).outcome is release.Outcome.PUBLISHED
    assert [call[0] for call in remote.calls] == ['push', 'pr']


def unlanded(remote, kind):
    def send(*args):
        remote.calls.append((kind, *args))
        raise OSError('possibly still in flight')
    return send


@pytest.mark.parametrize('when', ['before-push', 'before-open'])
def test_scope_change_blocks_publication_step(setup, when):
    reopen, publish, remote, _ = setup
    changed = replace(remote.feature,
                      backlogs=(*remote.feature.backlogs[:-1], Backlog(SECOND, (), (5, 6))))
    if when == 'before-push':
        remote.feature = changed
    else:
        push = remote.push

        def change_on_push(*args):
            push(*args)
            remote.feature = changed

        remote.push = change_on_push
    assert publish(reopen()).outcome is release.Outcome.BLOCKED
    assert [call[0] for call in remote.calls] == ([] if when == 'before-push' else ['push'])


def published(setup):
    reopen, publish, remote, _ = setup
    receipt = publish(reopen()).receipt
    remote.check = release.HostedCheck(PR, receipt.publication.commit, SOURCE, 'fixture-run', 'success')
    return reopen, remote, receipt


@pytest.mark.parametrize('problem', ['missing', 'failed', 'head-edited', 'wrong-source', 'wrong-sha', 'wrong-base',
                                     'wrong-delta', 'wrong-pr', 'pr-edited', 'base-advanced', 'pending-design', 'changed-backlog'])
def test_closeout_squash_requires_exact_hosted_check_and_current_backlog(setup, problem):
    reopen, remote, receipt = published(setup)
    check = remote.check
    if problem == 'missing':
        remote.check = None
    elif problem == 'failed':
        remote.check = replace(check, status='failure')
    elif problem == 'wrong-source':
        remote.check = replace(check, producer='candidate-controlled source')
    elif problem in ('wrong-sha', 'wrong-base', 'wrong-delta'):
        field = {'wrong-sha': 'head_sha', 'wrong-base': 'base_sha', 'wrong-delta': 'delta_sha256'}[problem]
        remote.check = replace(check, commit=replace(check.commit, **{field: '9' * len(getattr(check.commit, field))}))
    elif problem == 'wrong-pr':
        remote.check = replace(check, pr=OTHER)
    elif problem == 'head-edited':
        remote.prs = (replace(remote.prs[0], commit=replace(check.commit, head_sha='9' * 40)),)
    elif problem == 'pr-edited':
        remote.prs = (replace(remote.prs[0], closes=(FEATURE, IssueRef(FEATURE.repo, 5))),)
    elif problem == 'base-advanced':
        remote.current_base = '5' * 40
    elif problem == 'pending-design':
        children = dict(remote.feature.children)
        children[2] = DesignEvidence(IssueState.OPEN, False, 1, (), None)
        remote.feature = replace(remote.feature, children=children)
    else:
        # Still a completed backlog, but one story was superseded since publication.
        remote.feature = replace(remote.feature, backlogs=(*remote.feature.backlogs[:-1], Backlog(SECOND, (), (5, 6))))
    assert reopen().merge(receipt).outcome is not release.Outcome.VERIFIED
    assert [call[0] for call in remote.calls] == ['push', 'pr']


def test_other_pr_cannot_use_publication_receipt_even_with_green_check(setup):
    reopen, remote, receipt = published(setup)
    remote.prs = (replace(remote.prs[0], pr=OTHER),)
    remote.check = replace(remote.check, pr=OTHER)
    assert reopen().merge(replace(receipt, pr=OTHER)).outcome is release.Outcome.BLOCKED
    assert reopen().merge(receipt).outcome is release.Outcome.DISCREPANCY
    assert [call[0] for call in remote.calls] == ['push', 'pr']


def test_base_change_at_squash_boundary_is_rejected_without_side_effect(setup):
    reopen, remote, receipt = published(setup)
    send = remote.conditional_squash

    def change_base(command):
        remote.current_base = '7' * 40
        return send(command)

    remote.conditional_squash = change_base
    assert reopen().merge(receipt).outcome is release.Outcome.CHANGED_AT_WRITE
    assert remote.feature.state is IssueState.OPEN and remote.squash_commit is None
    assert reopen().merge(receipt).outcome is release.Outcome.BLOCKED
    assert [call[0] for call in remote.calls] == ['push', 'pr', 'squash']


@pytest.mark.parametrize('ambiguous', [False, True])
def test_closeout_and_redesign_share_writer_holds(setup, tmp_path, ambiguous):
    reopen, remote, receipt = published(setup)
    story_broker = MergeBroker(tmp_path / 'journal', MergePolicy(POLICY, frozenset(), 'ci', 'review', 'e2e'), remote)
    revision = RevisionRequest(FEATURE, IssueRef(FEATURE.repo, 5), SECOND, 1)
    if ambiguous:
        remote.conditional_squash = unlanded(remote, 'squash')
        assert reopen().merge(receipt).outcome is release.Outcome.DISCREPANCY
        assert story_broker.register_redesign(revision) is StoryOutcome.IN_FLIGHT
        assert reopen().merge(receipt).outcome is release.Outcome.DISCREPANCY
    else:
        assert story_broker.register_redesign(revision) is StoryOutcome.REGISTERED
        assert reopen().merge(receipt).outcome is release.Outcome.BLOCKED
    assert [call[0] for call in remote.calls] == ['push', 'pr'] + (['squash'] if ambiguous else [])


@pytest.mark.parametrize('lose', [None, 'squash'])
def test_verify_exact_squash_trailer_and_auto_close_with_or_without_reply(setup, lose):
    reopen, remote, receipt = published(setup)
    remote.lose = lose
    result = reopen().merge(receipt)
    assert result == release.Result(release.Outcome.VERIFIED, squash_sha=SQUASH)
    assert remote.squash_commit.message.endswith(f'\n\nRefs: #{FEATURE.number}')
    assert remote.closure == release.Closure(PR, SQUASH)
    assert reopen().merge(receipt) == result
    assert [call[0] for call in remote.calls] == ['push', 'pr', 'squash']


def test_failed_read_after_squash_is_unknown_and_only_retries_verification(setup):
    reopen, remote, receipt = published(setup)
    send, read = remote.conditional_squash, remote.merge_state

    def unknown(*args):
        raise OSError('synthetic read outage')

    def lose_read(command):
        reported = send(command)
        remote.merge_state = unknown
        return reported

    remote.conditional_squash = lose_read
    assert reopen().merge(receipt).outcome is release.Outcome.UNKNOWN
    assert reopen().merge(receipt).outcome is release.Outcome.UNKNOWN
    remote.merge_state = read
    assert reopen().merge(receipt).squash_sha == SQUASH
    assert [call[0] for call in remote.calls] == ['push', 'pr', 'squash']


@pytest.mark.parametrize('problem', ['closed-early', 'merged-early', 'wrong-sha', 'wrong-trailer',
                                     'wrong-parent', 'wrong-delta', 'other-closer', 'earlier-closure',
                                     'no-auto-close', 'ambiguous'])
def test_discrepancies_never_establish_completion_or_resend(setup, problem):
    reopen, remote, receipt = published(setup)
    send = remote.conditional_squash

    def faulty(command):
        reply = send(command)
        commit = remote.squash_commit
        if problem == 'wrong-trailer':
            remote.squash_commit = replace(commit, message='No trailers')
        elif problem in ('wrong-parent', 'wrong-delta'):
            field = 'base_sha' if problem == 'wrong-parent' else 'delta_sha256'
            remote.squash_commit = replace(commit, commit=replace(commit.commit, **{field: '0' * 40}))
        elif problem == 'other-closer':
            remote.close_feature(OTHER)
        elif problem == 'earlier-closure':
            remote.closure = replace(remote.closure, squash_sha=BASE)
        elif problem == 'no-auto-close':
            remote.feature = replace(remote.feature, state=IssueState.OPEN)
            remote.closure = None
        return reply

    if problem == 'closed-early':
        remote.close_feature(OTHER)
    elif problem == 'merged-early':
        remote.prs = (replace(remote.prs[0], state=PullRequestState.MERGED),)
    elif problem == 'wrong-sha':
        remote.accepted_sha = 'd' * 40
    elif problem == 'ambiguous':
        remote.land = False
        remote.lose = 'squash'
    else:
        remote.conditional_squash = faulty
    assert reopen().merge(receipt).outcome is release.Outcome.DISCREPANCY
    assert reopen().merge(receipt).outcome is release.Outcome.DISCREPANCY
    sends = [call for call in remote.calls if call[0] == 'squash']
    assert len(sends) == (0 if problem.endswith('early') else 1)
    if problem == 'no-auto-close':
        remote.close_feature(PR)
        assert reopen().merge(receipt).squash_sha == SQUASH
    if problem == 'ambiguous':
        (call,) = sends
        command = call[1]
        remote.ref = None  # verification must work after deletion of the topic branch
        remote.prs = (replace(remote.prs[0], state=PullRequestState.MERGED),)
        remote.squash_commit = release.Squash(replace(receipt.publication.commit, head_sha=SQUASH), command.message)
        remote.close_feature(PR)
        assert reopen().merge(receipt).squash_sha == SQUASH
