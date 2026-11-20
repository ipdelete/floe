import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from threading import Barrier

import pytest

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

    def call(request):
        # Separate connections/objects, one trusted ledger.
        launcher = Launcher(state_dir, tick, start_worker)
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

    def lose_reply():
        Launcher(state_dir, tick, start_worker).start_or_get(tick, REQUEST)
        raise TimeoutError("reply dropped")

    with pytest.raises(TimeoutError):
        lose_reply()

    restarted = Launcher(state_dir, tick, start_worker)
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

    launcher = Launcher(state_dir, tick, start_worker)
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
    starts = []
    launcher = Launcher(state_dir, tick, lambda *args: starts.append(args))
    with pytest.raises(LedgerUnavailable):
        launcher.start_or_get(tick, REQUEST)
    assert not starts
    assert stored_record(state_dir) is None


def test_failed_commit_never_starts_worker(state_dir):
    tick = StartAuthority()
    starts = []
    launcher = Launcher(state_dir, tick, lambda *args: starts.append(args))
    # A reader's shared lock allows a reserved writer to UPDATE, but keeps it
    # from acquiring the exclusive lock it needs to COMMIT in rollback mode.
    with closing(sqlite3.connect(state_dir / "ledger.sqlite3")) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT record FROM slot WHERE id = 1").fetchone()
        with pytest.raises(LedgerUnavailable):
            launcher.start_or_get(tick, REQUEST)
        assert not starts
    assert stored_record(state_dir) is None


@pytest.mark.parametrize("damage", ["missing", "invalid database", "invalid record", "missing slot"])
def test_missing_or_corrupt_initialized_ledger_is_not_treated_as_empty(state_dir, damage):
    tick = StartAuthority()
    starts = []
    Launcher(state_dir, tick, lambda *args: starts.append(args)).start_or_get(tick, REQUEST)
    assert len(starts) == 1
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

    launcher = Launcher(state_dir, tick, lambda *args: starts.append(args))
    for request in (REQUEST, OTHER):
        with pytest.raises(LedgerUnavailable):
            launcher.start_or_get(tick, request)
    assert len(starts) == 1
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

    with pytest.raises(TimeoutError):
        Launcher(state_dir, tick, start_worker).start_or_get(tick, REQUEST)

    launcher = Launcher(state_dir, tick, start_worker)
    [(original, _)] = starts
    assert launcher.start_or_get(tick, REQUEST) == original
    with pytest.raises(SlotOccupied):
        launcher.start_or_get(tick, replace(REQUEST, key=replace(REQUEST.key, attempt=2)))
    assert starts == [(original, REQUEST)]
