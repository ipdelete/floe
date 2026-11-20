from dataclasses import replace
from threading import Event, Thread

import floe
import pytest

from test_launcher import FakeManager
from test_readiness import FIRST, POLICY, design, feature, story
from tools.agentic_dev_loop.feature_tree import IssueRef
from tools.agentic_dev_loop.launcher import (
    AttemptKey,
    Launcher,
    StartAuthority,
    WorkerState,
    initialize_ledger,
)
from tools.agentic_dev_loop.readiness import Backlog, Reason, Role
from tools.agentic_dev_loop.tick import (
    AttemptInput,
    Cooldowns,
    EndpointUnavailable,
    FeatureScope,
    Observation,
    Outcome,
    RunNote,
    Tick,
    TickLimits,
)


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def scope(number, *, read_bucket="read", write_bucket="write"):
    return FeatureScope(IssueRef("example/floe", number), 1000, 1000 + number,
                        POLICY, read_bucket, write_bucket)


READY = scope(101)
ISSUE = IssueRef(READY.feature.repo, 6)
KEY = AttemptKey(READY.repo_id, READY.feature_id, 2006, Role.BUILDER, 1)


class Issues:
    def __init__(self):
        self.calls = []
        self.notes = {}
        self.read_faults = {}
        self.before_write = lambda: None
        self.after_write = lambda: None
        self.snapshots = {}
        self.pending_judgment = set()

    def read(self, scope, *, deadline):
        self.calls.append(("read", scope.feature, deadline))
        if fault := self.read_faults.get(scope.feature):
            fault(deadline)
        # Only the ready feature has children #5 and #6; other features get
        # disjoint issue numbers and stable IDs.
        ready_issue = 6 + (scope.feature.number - READY.feature.number) * 10
        blocked_issue = ready_issue - 1
        design_issue = ready_issue - 2
        evidence = feature(
            designs={design_issue: design(design_issue)},
            stories={blocked_issue: story(prerequisites=(ready_issue,)), ready_issue: story()},
            backlog=Backlog(FIRST, (blocked_issue, ready_issue)),
            pending_judgment=scope.feature in self.pending_judgment,
        )
        return Observation(evidence, {
            issue: AttemptInput(2000 + issue, self.snapshots.get(scope.feature, b"pinned inputs"),
                                self.notes.get(IssueRef(scope.feature.repo, issue)))
            for issue in (ready_issue, blocked_issue)
        })

    def mirror_if_missing(self, issue, note, *, deadline):
        self.calls.append(("write", issue, deadline))
        self.before_write()
        if issue in self.notes:  # fixture writer rechecks current state, not the read snapshot
            return False
        self.notes[issue] = note
        self.after_write()
        return True


class Admission:
    bucket = "launcher"

    def __init__(self, path, authority, manager):
        self.launcher = Launcher(path, authority, manager)
        self.authority = authority
        self.calls = []
        self.after_admission = lambda reservation: None

    def start_or_get(self, request, *, deadline):
        self.calls.append((request, deadline))
        reservation = self.launcher.start_or_get(self.authority, request)
        self.after_admission(reservation)
        return reservation


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "launcher"
    initialize_ledger(path)
    manager = FakeManager()
    authority = StartAuthority()
    issues, clock, cooldowns = Issues(), Clock(), Cooldowns()

    def new_tick(**limits):
        admission = Admission(path, authority, manager)
        return Tick(issues, admission, cooldowns, clock=clock, limits=TickLimits(**limits)), admission

    return new_tick, issues, manager, clock


def test_floe_tick_submits_only_eligible_work_and_does_not_wait(setup):
    new_tick, issues, manager, _ = setup
    tick, admission = new_tick()
    running, finish = Event(), Event()

    def fixture_worker():
        running.set()
        finish.wait(timeout=5)

    worker = Thread(target=fixture_worker)
    manager.on_start = lambda *_: worker.start()
    events, reports = [], []
    try:
        assert floe.run(tick.workflow([READY], reports.append), events.append)
        assert running.wait(timeout=1) and worker.is_alive()
        assert [request.key for request, _ in admission.calls] == [KEY]
        [result], = reports
        assert result.outcome is Outcome.MIRRORED
        assert [(b.issue, b.reason) for b in result.decision.blockers] == [(5, Reason.UNMET_PREREQUISITE)]
        [reservation] = manager.starts
        assert issues.notes == {ISSUE: RunNote(reservation)}
        assert events[-1].event == "run_completed" and events[-1].status == "succeeded"
    finally:
        finish.set()
        if worker.ident is not None:
            worker.join(timeout=2)


class TickKilled(BaseException):
    pass


@pytest.mark.parametrize("lost", [True, False])
def test_next_tick_rereads_and_recovers_exact_admission(setup, lost):
    new_tick, issues, manager, clock = setup
    tick, admission = new_tick()

    def lose_reply(_):
        raise TimeoutError("reply lost")

    def kill_tick():
        raise TickKilled

    if lost:
        admission.after_admission = lose_reply
        [result] = tick.reconcile([READY])
        assert result.outcome is Outcome.UNAVAILABLE
    else:
        issues.before_write = kill_tick
        with pytest.raises(TickKilled):
            tick.reconcile([READY])
        issues.before_write = lambda: None
    assert issues.notes == {}
    [original] = manager.starts
    clock.now += 60
    restarted, replay = new_tick()
    [result] = restarted.reconcile([READY])
    assert result.outcome is Outcome.MIRRORED
    assert result.reservation == original
    assert [r for r, _ in replay.calls] == [r for r, _ in admission.calls]
    assert [kind for kind, _, _ in issues.calls].count("read") == 2
    assert issues.notes == {ISSUE: RunNote(original)}
    assert manager.starts == [original]


@pytest.mark.parametrize("terminal", [True, False])
def test_writer_preserves_a_note_that_appears_after_the_read(setup, terminal):
    new_tick, issues, manager, _ = setup
    tick, _ = new_tick()

    def concurrent_note():
        [reservation] = manager.starts
        notes_reservation = reservation if terminal else replace(reservation, run_id="conflicting")
        issues.notes[ISSUE] = RunNote(notes_reservation, terminal=terminal)

    issues.before_write = concurrent_note
    [result] = tick.reconcile([READY])
    assert result.outcome is Outcome.NOTE_PRESENT
    [original] = manager.starts
    assert issues.notes[ISSUE] == RunNote(
        original if terminal else replace(original, run_id="conflicting"), terminal=terminal,
    )
    restarted, replay = new_tick()
    [result] = restarted.reconcile([READY])
    assert result.outcome is Outcome.NOTE_PRESENT
    assert not replay.calls
    assert [kind for kind, _, _ in issues.calls].count("write") == 1


def test_changed_external_inputs_do_not_replay_or_overwrite(setup):
    new_tick, issues, manager, clock = setup
    tick, admission = new_tick()

    def change_inputs(_):
        issues.snapshots[READY.feature] = b"new inputs"
        raise TimeoutError

    admission.after_admission = change_inputs
    tick.reconcile([READY])
    clock.now += 60
    restarted, _ = new_tick()
    [result] = restarted.reconcile([READY])
    assert result.outcome is Outcome.ADMISSION_REFUSED
    assert [kind for kind, _, _ in issues.calls] == ["read", "read"]
    assert not issues.notes
    assert len(manager.starts) == 1


def test_recovery_does_not_resubmit_when_fresh_readiness_blocks_work(setup):
    new_tick, issues, manager, clock = setup
    tick, admission = new_tick()

    def lose_reply(_):
        issues.pending_judgment.add(READY.feature)
        raise TimeoutError

    admission.after_admission = lose_reply
    tick.reconcile([READY])
    clock.now += 60
    restarted, replay = new_tick()
    [result] = restarted.reconcile([READY])
    assert result.outcome is Outcome.NO_WORK
    assert result.decision.blockers[0].reason is Reason.PENDING_JUDGMENT
    assert [kind for kind, _, _ in issues.calls] == ["read", "read"]
    assert not replay.calls and not issues.notes
    assert len(manager.starts) == 1


def test_lost_writeback_reply_checks_externally_before_doing_anything_again(setup):
    new_tick, issues, manager, _ = setup
    tick, admission = new_tick()

    def lose_reply():
        raise TimeoutError("write reply lost")

    issues.after_write = lose_reply
    [result] = tick.reconcile([READY])
    assert result.outcome is Outcome.UNAVAILABLE
    [original] = manager.starts
    assert result.reservation == original
    assert issues.notes == {ISSUE: RunNote(original)}

    restarted, replay = new_tick()
    [result] = restarted.reconcile([READY])
    assert result.outcome is Outcome.NOTE_PRESENT
    assert not replay.calls
    assert len(admission.calls) == 1 and manager.starts == [original]
    assert [kind for kind, _, _ in issues.calls] == ["read", "write", "read"]


def test_writeback_outage_only_defers_scopes_that_need_that_writer(setup):
    new_tick, issues, manager, clock = setup
    same_writer = scope(102)
    different_writer = scope(103, write_bucket="another-writer")
    tick, admission = new_tick(backoff_seconds=7)

    def blocked_write():
        raise TimeoutError

    issues.before_write = blocked_write
    assert tick.reconcile([READY])[0].outcome is Outcome.UNAVAILABLE
    [original] = manager.starts
    manager.complete(original, WorkerState.SUCCEEDED)
    manager.effects[original] = True
    issues.before_write = lambda: None

    clock.now = 106
    results = tick.reconcile([same_writer, different_writer])
    assert [result.outcome for result in results] == [Outcome.BACKOFF, Outcome.MIRRORED]
    assert [request.key.feature_id for request, _ in admission.calls] == [
        READY.feature_id, different_writer.feature_id,
    ]
    clock.now = 107
    assert tick.reconcile([READY])[0].reservation == original
    assert issues.notes[ISSUE] == RunNote(original)


def test_deadlines_batch_and_rate_limit_backoff_leave_independent_capacity_usable(setup):
    new_tick, issues, manager, clock = setup
    limited, shared, outside = scope(102), scope(103), scope(104)
    independent = replace(READY, read_bucket="independent", write_bucket="independent")

    def blocked(deadline):
        clock.now = deadline  # the fixture endpoint consumes its entire call budget
        raise EndpointUnavailable(retry_at=130)

    issues.read_faults[limited.feature] = blocked
    limits = dict(batch_size=3, tick_seconds=5, call_seconds=2, backoff_seconds=7)
    tick, admission = new_tick(**limits)
    results = tick.reconcile([limited, shared, independent, outside])
    assert [result.outcome for result in results] == [Outcome.UNAVAILABLE, Outcome.BACKOFF, Outcome.MIRRORED]
    assert issues.calls == [("read", limited.feature, 102), ("read", independent.feature, 104),
                            ("write", ISSUE, 104)]
    assert [deadline for _, deadline in admission.calls] == [104]
    [original] = manager.starts
    manager.complete(original, WorkerState.SUCCEEDED)
    manager.effects[original] = True

    # The next invocation is a new tick but the trusted fixture host still owns
    # the cooldown. Neither feature sharing the rate bucket calls it early.
    clock.now = 129
    restarted, replay = new_tick(**limits)
    assert [r.outcome for r in restarted.reconcile([limited, shared])] == [Outcome.BACKOFF] * 2
    assert len(issues.calls) == 3 and not replay.calls
    clock.now = 130
    del issues.read_faults[limited.feature]
    assert restarted.reconcile([limited])[0].outcome is Outcome.MIRRORED
    assert issues.calls[-2:] == [("read", limited.feature, 132),
                                 ("write", IssueRef(limited.feature.repo, 16), 132)]


def test_global_deadline_stops_further_calls_even_after_a_late_success(setup):
    new_tick, issues, manager, clock = setup
    tick, admission = new_tick(batch_size=3, tick_seconds=3, call_seconds=2)
    slow = scope(102, read_bucket="slow")

    def stall(deadline):
        clock.now = deadline
        raise TimeoutError

    def late_success(deadline):
        clock.now = deadline

    issues.read_faults[slow.feature] = stall
    issues.read_faults[READY.feature] = late_success
    results = tick.reconcile([slow, READY, scope(103)])
    assert [result.outcome for result in results] == [Outcome.UNAVAILABLE, Outcome.UNAVAILABLE]
    assert issues.calls == [("read", slow.feature, 102), ("read", READY.feature, 103)]
    assert not admission.calls and not manager.starts
