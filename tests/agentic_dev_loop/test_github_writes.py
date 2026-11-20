from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from time import monotonic

import pytest

from tools.agentic_dev_loop.github_writes import (
    CloseIssue, CreateIssue, CreatePR, Evidence, IssueRecord, IssueState, IssuesState,
    LinkSubIssue, NonApplication, Operation, Outcome, ParentState, PRRecord, PRState, Push, RefState,
    RunRecord, RunState, TransportUnavailable, WriteRun, Writer, initialize_writer,
)
from tools.agentic_dev_loop.launcher import AttemptKey, AttemptState, Reservation
from tools.agentic_dev_loop.readiness import Role

OLD, NEW, OTHER = (char * 40 for char in 'abc')
REPO = 670
REF = 'refs/heads/feature/29-46'
RESERVATION = Reservation(AttemptKey(REPO, 29, 460, Role.BUILDER, 1), 'run', 'unit', 'session')
RUNNING = RunRecord(101, RESERVATION, AttemptState.RESERVED)
TERMINAL = RunRecord(102, RESERVATION, AttemptState.SUCCEEDED)
ISSUES = IssuesState('story-46', ())
PR = PRState('master', REF, OLD, NEW, ())


CASES = [
    (Push(REF, OLD, NEW), RefState(REF, OLD), RefState(REF, NEW), RefState(REF, OTHER), NEW),
    (CreateIssue('story-46', 'Story', 'Body'), ISSUES,
     replace(ISSUES, matches=(IssueRecord(460, 'Story', 'Body'),)),
     replace(ISSUES, matches=(IssueRecord(460, 'Other', 'Body'),)), 460),
    (LinkSubIssue(29, 460), ParentState(29, 460, None), ParentState(29, 460, 29),
     ParentState(29, 460, 30), 460),
    (CloseIssue(460), IssueState(460, False), IssueState(460, True), IssueState(461, True), 460),
    (CreatePR('master', REF, OLD, NEW, 'Story', 'Closes #46'), PR,
     replace(PR, matches=(PRRecord(99, NEW, 'Story', 'Closes #46'),)),
     replace(PR, matches=(PRRecord(99, OTHER, 'Story', 'Closes #46'),)), 99),
    (WriteRun(RESERVATION, None, AttemptState.RESERVED), RunState(RESERVATION.key, None),
     RunState(RESERVATION.key, RUNNING), RunState(RESERVATION.key, TERMINAL), 101),
    (WriteRun(RESERVATION, RUNNING, AttemptState.SUCCEEDED), RunState(RESERVATION.key, RUNNING),
     RunState(RESERVATION.key, TERMINAL),
     RunState(RESERVATION.key, replace(TERMINAL, status=AttemptState.CANCELLED)), 102),
]


class Transport:
    def __init__(self, before, after):
        self.evidence = Evidence(REPO, before)
        self.after = after
        self.calls = []
        self.lose_reply = False
        self.delay = False

    def read(self, operation, *, deadline):
        self.calls.append(('read', operation))
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return self.evidence

    def send(self, operation, *, generation, deadline):
        self.calls.append(('send', operation, generation))
        if not self.delay:
            self.evidence = Evidence(REPO, self.after)
        if self.lose_reply:
            raise TransportUnavailable()


@pytest.fixture
def state(tmp_path):
    path = tmp_path / 'writer'
    initialize_writer(path)
    return path


def run(state, transport, operation):
    # Reopen the writer every time, as a new tick would.
    return Writer(state, REPO, transport).reconcile(operation, deadline=monotonic() + 10)


@pytest.mark.parametrize('change,before,after,conflict,receipt', CASES)
@pytest.mark.parametrize('scenario', ['present', 'absent', 'reply-lost', 'conflict', 'pending', 'unreadable'])
def test_reconciliation(state, change, before, after, conflict, receipt, scenario):
    transport = Transport(before, after)
    op = Operation('attempt/operation', REPO, change)
    if scenario == 'present':
        transport.evidence = Evidence(REPO, after)
    elif scenario == 'reply-lost':
        transport.lose_reply = True
    elif scenario == 'conflict':
        transport.evidence = Evidence(REPO, conflict)
    elif scenario == 'pending':
        transport.evidence = Evidence(REPO, before, pending=True)
    elif scenario == 'unreadable':
        transport.evidence = TransportUnavailable()

    result = run(state, transport, op)
    if scenario in ('present', 'absent', 'reply-lost'):
        assert (result.outcome, result.receipt) == (Outcome.VERIFIED, receipt)
        assert run(state, transport, op) == result
    else:
        assert result.outcome is {
            'conflict': Outcome.CONFLICT, 'pending': Outcome.IN_FLIGHT, 'unreadable': Outcome.UNKNOWN,
        }[scenario]
        assert result.receipt is None
    if scenario in ('absent', 'reply-lost'):
        assert transport.calls == [('read', op), ('send', op, 1), ('read', op)]
    else:
        assert transport.calls == [('read', op)]


@pytest.mark.parametrize('change,before,after,conflict,receipt', CASES)
def test_uncertain_send_requires_more_than_unchanged_state(state, change, before, after, conflict, receipt):
    transport = Transport(before, after)
    transport.delay = transport.lose_reply = True
    op = Operation('stable-id', REPO, change)
    assert run(state, transport, op).outcome is Outcome.IN_FLIGHT
    assert run(state, transport, op).outcome is Outcome.IN_FLIGHT
    if not isinstance(change, WriteRun):
        transport.evidence = Evidence(REPO, conflict, nonapplication=NonApplication(op.id, 1))
        assert run(state, transport, op).outcome is Outcome.CONFLICT
    transport.evidence = TransportUnavailable()
    assert run(state, transport, op).outcome is Outcome.UNKNOWN
    assert [call for call in transport.calls if call[0] == 'send'] == [('send', op, 1)]
    transport.evidence = Evidence(REPO, before, nonapplication=NonApplication(op.id, 1))
    if isinstance(change, Push):
        transport.delay = False
        assert run(state, transport, op).receipt == receipt
        assert [call for call in transport.calls if call[0] == 'send'] == [
            ('send', op, 1), ('send', op, 2),
        ]
    else:
        assert run(state, transport, op).outcome is Outcome.IN_FLIGHT
        transport.evidence = Evidence(REPO, after)
        assert run(state, transport, op).receipt == receipt
        assert [call for call in transport.calls if call[0] == 'send'] == [('send', op, 1)]


@pytest.mark.parametrize('change,before,after,changed', [
    (*CASES[0][:3], Push(REF + '-other', OLD, NEW)),
    (*CASES[0][:3], Push(REF, OLD, OTHER)),
    (*CASES[1][:3], CreateIssue('other-marker', 'Story', 'Body')),
    (*CASES[2][:3], LinkSubIssue(30, 460)),
    (*CASES[3][:3], CloseIssue(461)),
    (*CASES[4][:3], replace(CASES[4][0], base='other-base')),
    (*CASES[4][:3], replace(CASES[4][0], head_sha=OTHER)),
    (*CASES[5][:3], WriteRun(replace(RESERVATION, key=replace(RESERVATION.key, attempt=2)),
                            None, AttemptState.RESERVED)),
    (*CASES[6][:3], WriteRun(RESERVATION, RUNNING, AttemptState.FAILED)),
])
def test_id_is_bound_even_if_first_read_failed(state, change, before, after, changed):
    transport = Transport(before, after)
    transport.evidence = TransportUnavailable()
    op = Operation('stable-id', REPO, change)
    assert run(state, transport, op).outcome is Outcome.UNKNOWN
    assert run(state, transport, replace(op, change=changed)).outcome is Outcome.CONFLICT
    assert run(state, transport, replace(op, repo_id=REPO + 1)).outcome is Outcome.CONFLICT
    assert transport.calls == [('read', op)]


@pytest.mark.parametrize('wrong', [
    Evidence(REPO + 1, RefState(REF, OLD)),
    Evidence(REPO, RefState(REF + '-wrong', OLD)),
    Evidence(REPO, ParentState(30, 460, None)),
    Evidence(REPO, replace(PR, head=REF + '-wrong')),
    Evidence(REPO, RunState(replace(RESERVATION.key, issue_id=461), None)),
])
def test_evidence_from_wrong_repository_or_target_cannot_authorize_write(state, wrong):
    change = {RefState: CASES[0][0], ParentState: CASES[2][0], PRState: CASES[4][0],
              RunState: CASES[5][0]}[type(wrong.snapshot)]
    transport = Transport(None, None)
    transport.evidence = wrong
    op = Operation('stable-id', REPO, change)
    assert run(state, transport, op).outcome is Outcome.CONFLICT
    assert transport.calls == [('read', op)]


def test_terminal_write_and_stale_tick_are_serialized_without_comment_cas(state):
    entering, release = Event(), Event()

    class BlockingTransport(Transport):
        def send(self, operation, *, generation, deadline):
            entering.set()
            assert release.wait(5)
            super().send(operation, generation=generation, deadline=deadline)

    change, before, after = CASES[6][:3]
    transport = BlockingTransport(before, after)
    terminal = Operation('terminal', REPO, change)
    stale = Operation('initial', REPO, CASES[5][0])
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run, state, transport, terminal)
        try:
            assert entering.wait(5)
            assert run(state, transport, stale).outcome is Outcome.BUSY
        finally:
            release.set()
        assert task.result(timeout=5).receipt == TERMINAL.comment_id
    transport.evidence = Evidence(REPO, RunState(RESERVATION.key, None))  # stale remote view
    assert run(state, transport, stale).outcome is Outcome.CONFLICT
    assert [call for call in transport.calls if call[0] == 'send'] == [('send', terminal, 1)]


def test_acknowledgement_is_not_a_verified_result_and_a_new_id_cannot_bypass_pending(state):
    change, before, after = CASES[1][:3]
    transport = Transport(before, after)
    transport.delay = True
    first = Operation('first', REPO, change)
    assert run(state, transport, first).outcome is Outcome.IN_FLIGHT
    assert run(state, transport, replace(first, id='second')).outcome is Outcome.IN_FLIGHT
    assert [call for call in transport.calls if call[0] == 'send'] == [('send', first, 1)]


def test_push_needs_nonapplication_proof_for_its_current_generation(state):
    change, before, after = CASES[0][:3]
    transport = Transport(before, after)
    transport.delay = transport.lose_reply = True
    op = Operation('push-id', REPO, change)
    assert run(state, transport, op).outcome is Outcome.IN_FLIGHT
    transport.evidence = Evidence(REPO, before, nonapplication=NonApplication('other-id', 1))
    assert run(state, transport, op).outcome is Outcome.IN_FLIGHT
    transport.evidence = Evidence(REPO, before, nonapplication=NonApplication(op.id, 1))
    assert run(state, transport, op).outcome is Outcome.IN_FLIGHT  # send 2 also ambiguous
    assert run(state, transport, op).outcome is Outcome.IN_FLIGHT  # same proof cannot authorize send 3
    assert [call for call in transport.calls if call[0] == 'send'] == [('send', op, 1), ('send', op, 2)]


def test_checkpoint_survives_interruption_before_postflight(state):
    class Interrupted(BaseException):
        pass

    class InterruptedTransport(Transport):
        def send(self, operation, *, generation, deadline):
            super().send(operation, generation=generation, deadline=deadline)
            raise Interrupted()

    change, before, after = CASES[6][:3]
    transport = InterruptedTransport(before, after)
    op = Operation('terminal', REPO, change)
    with pytest.raises(Interrupted):
        run(state, transport, op)
    stale = Operation('initial', REPO, CASES[5][0])
    assert run(state, transport, stale).outcome is Outcome.IN_FLIGHT
    assert run(state, transport, op).receipt == TERMINAL.comment_id
    assert transport.calls == [('read', op), ('send', op, 1), ('read', op)]


def test_conflicting_terminal_evidence_cannot_be_replaced_by_a_stale_read(state):
    change, before, after, conflict = CASES[6][:4]
    transport = Transport(before, after)
    op = Operation('terminal', REPO, change)
    transport.evidence = Evidence(REPO, conflict)
    assert run(state, transport, op).outcome is Outcome.CONFLICT
    for snapshot in (before, after):
        transport.evidence = Evidence(REPO, snapshot)
        assert run(state, transport, op).outcome is Outcome.CONFLICT
    assert transport.calls == [('read', op)] * 3


def test_pending_terminal_observation_cannot_pin_the_terminal_result(state):
    change, before, after, conflict = CASES[6][:4]
    transport = Transport(before, after)
    op = Operation('terminal', REPO, change)
    transport.evidence = Evidence(REPO, conflict, pending=True)
    assert run(state, transport, op).outcome is Outcome.IN_FLIGHT
    transport.evidence = Evidence(REPO, before)
    assert run(state, transport, op).receipt == TERMINAL.comment_id
    assert transport.calls == [('read', op), ('read', op), ('send', op, 1), ('read', op)]


@pytest.mark.parametrize('change,snapshot', [
    (CASES[1][0], replace(ISSUES, matches=(IssueRecord(1, 'Story', 'Body'), IssueRecord(2, 'Story', 'Body')))),
    (CASES[4][0], replace(PR, base_sha=OTHER)),
    (CASES[4][0], replace(PR, head_sha=OTHER)),
    (CASES[6][0], RunState(RESERVATION.key, replace(RUNNING, comment_id=222))),
])
def test_result_must_be_unique_and_original_preconditions_must_match(state, change, snapshot):
    transport = Transport(snapshot, None)
    op = Operation('id', REPO, change)
    assert run(state, transport, op).outcome is Outcome.CONFLICT
    assert transport.calls == [('read', op)]


def test_expired_deadline_and_missing_journal_do_not_authorize_calls(state):
    change, before, after = CASES[0][:3]
    transport = Transport(before, after)
    op = Operation('id', REPO, change)
    assert Writer(state, REPO, transport).reconcile(op, deadline=monotonic() - 1).outcome is Outcome.UNKNOWN
    assert run(state / 'wrong-path', transport, op).outcome is Outcome.UNKNOWN
    assert transport.calls == []
