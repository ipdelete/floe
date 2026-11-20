import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from threading import Barrier

import pytest

from tools.agentic_dev_loop import launcher as lifecycle
from tools.agentic_dev_loop.launcher import (
    AttemptKey,
    ConflictingRequest,
    Launcher,
    LedgerUnavailable,
    SlotOccupied,
    StartAuthority,
    StartRequest,
    UnauthorizedStart,
    initialize_ledger,
)
from tools.agentic_dev_loop.readiness import Role


REQUEST = StartRequest(AttemptKey(100, 200, 300, Role.BUILDER, 1), "a" * 40, b"snapshot")
OTHER = replace(REQUEST, key=replace(REQUEST.key, repo_id=101))
RETRY = replace(REQUEST, key=replace(REQUEST.key, attempt=2))


class FakeManager:
    def __init__(self, on_start=None):
        self.on_start = on_start
        self.starts = []
        self.stops = []
        self.units = {}
        self.evidence = {}
        self.effects = {}
        self.inspect_error = False
        self.stop_error = False
        self.survivor = False

    def start(self, reservation, request):
        self.starts.append(reservation)
        self.units[reservation.unit_id] = reservation
        if self.on_start is not None:
            self.on_start(reservation, request)

    def inspect(self, reservation):
        if self.inspect_error:
            raise lifecycle.WorkerUnavailable("manager query failed")
        owner = self.units.get(reservation.unit_id)
        if owner is not None and owner != reservation:
            return lifecycle.WorkerState.FOREIGN
        if reservation in self.evidence:
            return self.evidence[reservation]
        if owner == reservation:
            return lifecycle.WorkerState.RUNNING
        return lifecycle.WorkerState.MISSING

    def stop(self, reservation):
        assert self.units.get(reservation.unit_id) == reservation
        self.stops.append(reservation)
        if self.stop_error:
            raise lifecycle.WorkerUnavailable("stop failed")
        if not self.survivor:
            del self.units[reservation.unit_id]
            self.evidence[reservation] = lifecycle.WorkerState.CLEAN

    def effects_reconciled(self, reservation):
        return self.effects.get(reservation)

    def complete(self, reservation, state):
        del self.units[reservation.unit_id]
        self.evidence[reservation] = state


@pytest.fixture
def state_dir(tmp_path):
    path = tmp_path / "launcher"
    initialize_ledger(path)
    return path


def stored_record(state_dir):
    # Use an independent connection; uncommitted writes must not count.
    with closing(sqlite3.connect(state_dir / "ledger.sqlite3")) as conn:
        [record] = conn.execute("SELECT record FROM slot WHERE id = 1").fetchone()
    return json.loads(record) if record is not None else None


def assert_committed_ids(state_dir, reservation):
    record = stored_record(state_dir)
    assert record is not None, "no committed reservation"
    assert (record["run_id"], record["unit_id"], record["session_id"]) == (
        reservation.run_id, reservation.unit_id, reservation.session_id,
    )


@pytest.mark.parametrize("requests", [(REQUEST, REQUEST), (REQUEST, REQUEST, OTHER)])
def test_concurrent_admission_commits_one_reservation_before_start(state_dir, requests):
    tick = StartAuthority()
    ready = Barrier(len(requests), timeout=5)
    starts = []

    def start_worker(reservation, request):
        starts.append((reservation, request))
        assert_committed_ids(state_dir, reservation)

    manager = FakeManager(start_worker)

    def call(request):
        # Separate connections/objects, one trusted ledger.
        launcher = Launcher(state_dir, tick, manager)
        ready.wait()
        try:
            reservation = launcher.start_or_get(tick, request)
        except SlotOccupied:
            return None
        assert_committed_ids(state_dir, reservation)
        return reservation

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        replies = list(executor.map(call, requests, timeout=10))

    [(reservation, winning_request)] = starts
    assert reservation.key == winning_request.key
    assert reservation.run_id and reservation.unit_id and reservation.session_id
    assert replies == [reservation if request == winning_request else None for request in requests]


def test_resend_after_lost_reply_recovers_ids_and_rejects_changed_inputs(state_dir):
    tick = StartAuthority()
    starts = []

    def start_worker(reservation, request):
        starts.append((reservation, request))

    manager = FakeManager(start_worker)

    def lose_reply():
        Launcher(state_dir, tick, manager).start_or_get(tick, REQUEST)
        raise TimeoutError("reply dropped")

    with pytest.raises(TimeoutError):
        lose_reply()

    restarted = Launcher(state_dir, tick, manager)
    [(original, _)] = starts
    assert restarted.start_or_get(tick, REQUEST) == original
    for changed in (replace(REQUEST, approved_revision="b" * 40),
                    replace(REQUEST, input_snapshot=b"different snapshot")):
        with pytest.raises(ConflictingRequest):
            restarted.start_or_get(tick, changed)
    assert stored_record(state_dir)["run_id"] == original.run_id
    assert starts == [(original, REQUEST)]


def test_unauthorized_caller_and_worker_have_no_ledger_write_interface(state_dir):
    tick = StartAuthority()
    worker = StartAuthority()
    starts = []

    def start_worker(reservation, request):
        starts.append((reservation, request))
        with pytest.raises(FrozenInstanceError):
            reservation.run_id = "replacement"
        for replay in (request, replace(request, input_snapshot=b"replacement")):
            with pytest.raises(UnauthorizedStart):
                launcher.start_or_get(worker, replay)

    launcher = Launcher(state_dir, tick, FakeManager(start_worker))
    with pytest.raises(UnauthorizedStart):
        launcher.start_or_get(worker, REQUEST)
    assert not starts
    assert stored_record(state_dir) is None

    original = launcher.start_or_get(tick, REQUEST)
    assert launcher.start_or_get(tick, REQUEST) == original
    assert stored_record(state_dir)["run_id"] == original.run_id
    assert starts == [(original, REQUEST)]


def test_failed_reservation_write_never_starts_worker(state_dir):
    with closing(sqlite3.connect(state_dir / "ledger.sqlite3")) as conn, conn:
        conn.execute("""CREATE TRIGGER reject_write BEFORE UPDATE ON slot
                       BEGIN SELECT RAISE(FAIL, 'fixture write failure'); END""")

    tick = StartAuthority()
    manager = FakeManager()
    launcher = Launcher(state_dir, tick, manager)
    with pytest.raises(LedgerUnavailable):
        launcher.start_or_get(tick, REQUEST)
    assert not manager.starts
    assert stored_record(state_dir) is None


def test_failed_commit_never_starts_worker(state_dir):
    tick = StartAuthority()
    manager = FakeManager()
    launcher = Launcher(state_dir, tick, manager)
    # A reader's shared lock allows a reserved writer to UPDATE, but keeps it
    # from acquiring the exclusive lock it needs to COMMIT in rollback mode.
    with closing(sqlite3.connect(state_dir / "ledger.sqlite3")) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT record FROM slot WHERE id = 1").fetchone()
        with pytest.raises(LedgerUnavailable):
            launcher.start_or_get(tick, REQUEST)
        assert not manager.starts
    assert stored_record(state_dir) is None


@pytest.mark.parametrize("damage", ["missing", "invalid database", "invalid record", "missing slot"])
def test_missing_or_corrupt_initialized_ledger_is_not_treated_as_empty(state_dir, damage):
    tick = StartAuthority()
    manager = FakeManager()
    Launcher(state_dir, tick, manager).start_or_get(tick, REQUEST)
    assert len(manager.starts) == 1
    db = state_dir / "ledger.sqlite3"
    if damage == "missing":
        db.unlink()
    elif damage == "invalid database":
        db.write_bytes(b"not a SQLite database")
    else:
        with closing(sqlite3.connect(db)) as conn, conn:
            if damage == "missing slot":
                conn.execute("DELETE FROM slot")
            else:
                conn.execute("UPDATE slot SET record = '{}'")

    launcher = Launcher(state_dir, tick, manager)
    for request in (REQUEST, OTHER):
        with pytest.raises(LedgerUnavailable):
            launcher.start_or_get(tick, request)
    assert len(manager.starts) == 1
    if damage == "missing":
        assert not db.exists()
        with pytest.raises(LedgerUnavailable):
            initialize_ledger(state_dir)
        assert not db.exists()


def test_uncertain_start_keeps_slot_and_does_not_repeat_adapter_call(state_dir):
    tick = StartAuthority()
    starts = []

    def start_worker(reservation, request):
        starts.append((reservation, request))
        raise TimeoutError("start outcome unknown")

    manager = FakeManager(start_worker)
    with pytest.raises(TimeoutError):
        Launcher(state_dir, tick, manager).start_or_get(tick, REQUEST)

    launcher = Launcher(state_dir, tick, manager)
    [(original, _)] = starts
    assert launcher.start_or_get(tick, REQUEST) == original
    with pytest.raises(SlotOccupied):
        launcher.start_or_get(tick, RETRY)
    assert starts == [(original, REQUEST)]


class Interrupted(BaseException):
    pass


@pytest.mark.parametrize("commit,after_commit", [(1, False), (1, True), (2, True)])
def test_restart_around_reservation_and_dispatch_commits(state_dir, monkeypatch, commit, after_commit):
    tick = StartAuthority()
    manager = FakeManager()
    connect = lifecycle._connect
    commits = 0

    class InterruptCommit:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            nonlocal commits
            if sql == "COMMIT":
                commits += 1
                if commits == commit:
                    if after_commit:
                        self.connection.execute(sql)
                    raise Interrupted
            return self.connection.execute(sql, *args)

        def close(self):
            self.connection.close()

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "_connect", lambda *a, **kw: InterruptCommit(connect(*a, **kw)))
        with pytest.raises(Interrupted):
            Launcher(state_dir, tick, manager).start_or_get(tick, REQUEST)
    record = stored_record(state_dir)
    assert (record is not None) is after_commit
    assert manager.starts == []

    restarted = Launcher(state_dir, tick, manager)
    original = restarted.start_or_get(tick, REQUEST)
    if after_commit:
        assert original.run_id == record["run_id"]
        assert original.session_id == record["session_id"]
    assert restarted.start_or_get(tick, REQUEST) == original
    if commit == 2:
        # A committed dispatch claim with no receipt is intentionally ambiguous.
        assert restarted.status(tick, REQUEST.key).state is lifecycle.AttemptState.UNKNOWN
        with pytest.raises(SlotOccupied):
            restarted.start_or_get(tick, OTHER)
        assert manager.starts == []
    else:
        assert manager.starts == [original]


def test_restart_after_dispatch_and_completion_before_lookup(state_dir):
    def lost_receipt(*args):
        raise Interrupted

    tick = StartAuthority()
    manager = FakeManager(lost_receipt)
    with pytest.raises(Interrupted):
        Launcher(state_dir, tick, manager).start_or_get(tick, REQUEST)
    [original] = manager.starts
    restarted = Launcher(state_dir, tick, manager)
    assert restarted.start_or_get(tick, REQUEST) == original
    assert restarted.status(tick, original.run_id).state is lifecycle.AttemptState.RUNNING

    manager.complete(original, lifecycle.WorkerState.SUCCEEDED)
    assert original.unit_id not in manager.units  # collected before any terminal lookup
    manager.effects[original] = True
    restarted = Launcher(state_dir, tick, manager)
    result = restarted.status(tick, REQUEST.key)
    assert result.reservation == original
    assert result.state is lifecycle.AttemptState.SUCCEEDED
    assert not result.slot_reserved

    manager.evidence.clear()  # lookup must now rely on the ledger
    manager.on_start = None
    another = restarted.start_or_get(tick, OTHER)
    assert Launcher(state_dir, tick, manager).status(tick, original.run_id) == result
    assert restarted.start_or_get(tick, REQUEST) == original
    with pytest.raises(ConflictingRequest):
        restarted.start_or_get(tick, replace(REQUEST, input_snapshot=b"changed"))
    assert manager.starts == [original, another]


@pytest.mark.parametrize("problem", ["missing", "foreign", "failed inspection", "lost ledger"])
def test_uncertain_state_blocks_admission_and_resume(state_dir, problem):
    tick = StartAuthority()
    manager = FakeManager()
    original = Launcher(state_dir, tick, manager).start_or_get(tick, REQUEST)
    if problem == "missing":
        del manager.units[original.unit_id]
    elif problem == "foreign":
        manager.units[original.unit_id] = replace(original, session_id="foreign")
    elif problem == "failed inspection":
        manager.inspect_error = True
    else:
        (state_dir / "ledger.sqlite3").unlink()

    restarted = Launcher(state_dir, tick, manager)
    if problem == "lost ledger":
        with pytest.raises(LedgerUnavailable):
            restarted.status(tick, original.run_id)
        with pytest.raises(LedgerUnavailable):
            restarted.start_or_get(tick, REQUEST)
    else:
        assert restarted.status(tick, original.run_id).state is lifecycle.AttemptState.UNKNOWN
        assert restarted.start_or_get(tick, REQUEST) == original
        with pytest.raises(SlotOccupied):
            restarted.start_or_get(tick, OTHER)
        assert restarted.cancel(tick, original.run_id).slot_reserved
    assert manager.starts == [original]
    assert manager.stops == []


def test_retry_requires_reconciled_effects_and_explicit_permission(state_dir):
    tick = StartAuthority()
    manager = FakeManager()
    launcher = Launcher(state_dir, tick, manager)
    with pytest.raises(lifecycle.RetryNotAllowed):
        launcher.start_or_get(tick, RETRY, allow_retry=True)
    original = launcher.start_or_get(tick, REQUEST)
    manager.complete(original, lifecycle.WorkerState.FAILED)
    with pytest.raises(SlotOccupied):
        launcher.start_or_get(tick, RETRY, allow_retry=True)
    result = launcher.status(tick, original.run_id)
    assert result.state is lifecycle.AttemptState.FAILED
    assert result.slot_reserved

    manager.evidence.clear()
    manager.effects[original] = True
    restarted = Launcher(state_dir, tick, manager)
    with pytest.raises(lifecycle.RetryNotAllowed):
        restarted.start_or_get(tick, RETRY)
    assert not restarted.status(tick, original.run_id).slot_reserved
    retry = restarted.start_or_get(tick, RETRY, allow_retry=True)
    assert restarted.start_or_get(tick, RETRY) == retry
    assert manager.starts == [original, retry]


@pytest.mark.parametrize("problem", ["survivor", "failed cleanup", "unresolved effects"])
def test_cancel_rechecks_only_owned_run_and_retains_slot_until_clean(state_dir, problem):
    tick = StartAuthority()
    manager = FakeManager()
    launcher = Launcher(state_dir, tick, manager)
    original = launcher.start_or_get(tick, REQUEST)
    unrelated = replace(original, unit_id="unrelated.service", run_id="unrelated")
    manager.units[unrelated.unit_id] = unrelated
    manager.remote_writes = {original.run_id: "already published"}
    manager.effects[original] = problem != "unresolved effects"
    manager.survivor = problem == "survivor"
    manager.stop_error = problem == "failed cleanup"

    with pytest.raises(UnauthorizedStart):
        launcher.cancel(StartAuthority(), original.run_id)
    assert manager.stops == []
    assert launcher.status(tick, REQUEST.key).state is lifecycle.AttemptState.RUNNING
    assert launcher.cancel(tick, REQUEST.key).slot_reserved
    restarted = Launcher(state_dir, tick, manager)
    with pytest.raises(SlotOccupied):
        restarted.start_or_get(tick, OTHER)

    manager.effects[original] = True
    manager.survivor = manager.stop_error = False
    result = restarted.cancel(tick, original.run_id)
    assert result.state is lifecycle.AttemptState.CANCELLED
    assert not result.slot_reserved
    assert restarted.cancel(tick, REQUEST.key) == result
    assert manager.stops == [original] * (1 if problem == "unresolved effects" else 2)
    assert manager.units == {unrelated.unit_id: unrelated}
    assert manager.remote_writes == {original.run_id: "already published"}
    assert restarted.start_or_get(tick, OTHER).key == OTHER.key


def test_restart_after_stop_before_receipt_does_not_repeat_stop(state_dir, monkeypatch):
    tick = StartAuthority()
    manager = FakeManager()
    launcher = Launcher(state_dir, tick, manager)
    original = launcher.start_or_get(tick, REQUEST)
    manager.effects[original] = True
    stop = manager.stop

    def lose_stop_receipt(reservation):
        stop(reservation)
        raise Interrupted

    monkeypatch.setattr(manager, "stop", lose_stop_receipt)
    with pytest.raises(Interrupted):
        launcher.cancel(tick, original.run_id)

    restarted = Launcher(state_dir, tick, manager)
    # Not even a status lookup is needed before admitting the next clean run.
    another = restarted.start_or_get(tick, OTHER)
    result = restarted.status(tick, REQUEST.key)
    assert result.state is lifecycle.AttemptState.CANCELLED
    assert not result.slot_reserved
    assert restarted.cancel(tick, original.run_id) == result
    assert manager.stops == [original]
    assert manager.starts == [original, another]
    assert manager.units == {another.unit_id: another}
