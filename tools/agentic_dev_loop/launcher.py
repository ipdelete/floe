"""Trusted admission and recovery for fixture workers, one slot per ledger.

Only trusted code constructs the launcher and its adapter. Keep the ledger in
an operator-controlled parent directory outside worker access. The worker
handoff consists solely of value objects, not a ledger or start capability.
This in-process interface does not isolate arbitrary Python or authenticate a
remote caller. A reservation is not evidence that a worker started.
"""

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from .readiness import Role


@dataclass(frozen=True, slots=True)
class AttemptKey:
    repo_id: int
    feature_id: int
    issue_id: int
    role: Role
    attempt: int


@dataclass(frozen=True, slots=True)
class StartRequest:
    key: AttemptKey
    approved_revision: str | None
    input_snapshot: bytes


@dataclass(frozen=True, slots=True)
class Reservation:
    key: AttemptKey
    run_id: str
    unit_id: str
    session_id: str


class WorkerState(StrEnum):
    RUNNING = "running"
    MISSING = "missing"
    FOREIGN = "foreign"
    CLEAN = "clean"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkerUnavailable(Exception):
    """The adapter could not complete its operation or verify the evidence."""


class WorkerManager(Protocol):
    """Trusted, bounded operations; must not call back into the launcher.

    Evidence binds the exact run, unit and session. CLEAN confirms that no
    owned process or pending start remains; MISSING does not. SUCCEEDED and
    FAILED additionally require verified completion, not just worker exit.
    Keep cleanup/completion evidence available across unit collection until
    the launcher can persist it. A failed operation may have taken effect.
    """

    def start(self, reservation: Reservation, request: StartRequest) -> None:
        """Start only this reservation; refuse a foreign unit collision."""
        ...

    def inspect(self, reservation: Reservation) -> WorkerState:
        """Return verified evidence or raise WorkerUnavailable."""
        ...

    def stop(self, reservation: Reservation) -> None:
        """Recheck ownership at stop time; never target a foreign unit."""
        ...

    def effects_reconciled(self, reservation: Reservation) -> bool | None:
        """Read external state; True verifies effects, without undoing writes."""
        ...


class AttemptState(StrEnum):
    RESERVED = "reserved"
    UNKNOWN = "unknown"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AttemptStatus:
    reservation: Reservation
    state: AttemptState
    slot_reserved: bool


@dataclass(frozen=True, slots=True)
class _Record:
    reservation: Reservation
    fingerprint: str
    state: AttemptState = AttemptState.RESERVED

    def encode(self) -> str:
        return json.dumps({**asdict(self.reservation), "fingerprint": self.fingerprint,
                           "state": self.state})


class StartAuthority:
    """Identity capability for start, status and cancellation, not worker input."""

    __slots__ = ()


class AdmissionError(Exception):
    """The launcher did not admit the request."""


class UnauthorizedStart(AdmissionError):
    pass


class SlotOccupied(AdmissionError):
    pass


class ConflictingRequest(AdmissionError):
    pass


class LedgerUnavailable(AdmissionError):
    pass


class AttemptNotFound(AdmissionError):
    pass


class RetryNotAllowed(AdmissionError):
    pass


_LEDGER_NAME = "ledger.sqlite3"
_FORMAT_VERSION = 2
_TERMINAL = frozenset({AttemptState.SUCCEEDED, AttemptState.FAILED, AttemptState.CANCELLED})


def _connect(path: Path, *, mode: str) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, autocommit=True, timeout=2)


def initialize_ledger(state_dir: Path) -> None:
    """Provision a new private state directory once, never repair lost state."""
    path = state_dir.absolute() / _LEDGER_NAME
    try:
        path.parent.mkdir(mode=0o700)
        with closing(_connect(path, mode="rwc")) as conn:
            conn.execute("PRAGMA synchronous=EXTRA")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE slot (id INTEGER PRIMARY KEY CHECK (id = 1), record TEXT) STRICT")
            conn.execute("INSERT INTO slot VALUES (1, NULL)")
            conn.execute("""CREATE TABLE completed (
                         attempt_key TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL,
                         record TEXT NOT NULL) STRICT""")
            conn.execute(f"PRAGMA user_version={_FORMAT_VERSION}")
            conn.execute("COMMIT")
    except (OSError, sqlite3.Error) as exc:
        raise LedgerUnavailable("could not initialize a new launcher ledger") from exc


def _valid_key(key: object) -> bool:
    return (isinstance(key, AttemptKey) and isinstance(key.role, Role)
            and all(type(value) is int and value > 0
                    for value in (key.repo_id, key.feature_id, key.issue_id, key.attempt)))


def _hex(value: object, size: int) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]+", value) is not None and len(value) == size


def _fingerprint(request: StartRequest) -> str:
    # Persist the binding to the exact snapshot bytes without storing the snapshot.
    if (not isinstance(request, StartRequest) or not _valid_key(request.key)
            or (request.approved_revision is not None and not _hex(request.approved_revision, 40))
            or type(request.input_snapshot) is not bytes):
        raise ValueError("invalid start request")
    snapshot_digest = hashlib.sha256(request.input_snapshot).hexdigest()
    payload = json.dumps([request.approved_revision, snapshot_digest]).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _unit_id(run_id: str) -> str:
    return f"floe-loop-worker-{run_id}.service"


def _decode(record: str) -> _Record:
    try:
        fields = json.loads(record)
        if not isinstance(fields, dict):
            raise ValueError("expected a record")
        fingerprint = fields.pop("fingerprint")
        state = AttemptState(fields.pop("state"))
        key = fields.pop("key")
        key["role"] = Role(key["role"])
        reservation = Reservation(key=AttemptKey(**key), **fields)
        if (not _valid_key(reservation.key) or not _hex(fingerprint, 64)
                or not all(_valid_id(value) for value in (reservation.run_id, reservation.session_id))
                or reservation.unit_id != _unit_id(reservation.run_id)
                or state is AttemptState.RUNNING):
            raise ValueError("invalid record")
        return _Record(reservation, fingerprint, state)
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise LedgerUnavailable("invalid launcher ledger record") from exc


def _valid_id(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _key_index(key: AttemptKey) -> str:
    return json.dumps(asdict(key), sort_keys=True)


def _matches(record: _Record, target: AttemptKey | str) -> bool:
    return target == record.reservation.key or target == record.reservation.run_id


def _active(conn: sqlite3.Connection) -> _Record | None:
    row = conn.execute("SELECT record FROM slot WHERE id = 1").fetchone()
    if row is None:
        raise LedgerUnavailable("launcher slot record is missing")
    return _decode(row[0]) if row[0] is not None else None


def _set_active(conn: sqlite3.Connection, record: _Record | None) -> None:
    updated = conn.execute("UPDATE slot SET record = ? WHERE id = 1",
                           (record.encode() if record is not None else None,))
    if updated.rowcount != 1:
        raise LedgerUnavailable("launcher slot was not written")


def _completed(conn: sqlite3.Connection, target: AttemptKey | str) -> _Record | None:
    if isinstance(target, AttemptKey):
        row = conn.execute("SELECT record FROM completed WHERE attempt_key = ?",
                           (_key_index(target),)).fetchone()
    else:
        row = conn.execute("SELECT record FROM completed WHERE run_id = ?", (target,)).fetchone()
    if row is None:
        return None
    record = _decode(row[0])
    if record.state not in _TERMINAL or not _matches(record, target):
        raise LedgerUnavailable("invalid completed attempt")
    return record


def _release(conn: sqlite3.Connection, record: _Record) -> AttemptStatus:
    reservation = record.reservation
    conn.execute("INSERT INTO completed VALUES (?, ?, ?)",
                 (_key_index(reservation.key), reservation.run_id, record.encode()))
    _set_active(conn, None)
    return AttemptStatus(reservation, record.state, slot_reserved=False)


class Launcher:
    def __init__(self, state_dir: Path, start_authority: StartAuthority, manager: WorkerManager) -> None:
        if not isinstance(start_authority, StartAuthority):
            raise ValueError("expected a trusted start capability")
        self._path = state_dir.absolute() / _LEDGER_NAME
        self._start_authority = start_authority
        self._manager = manager

    def _authorize(self, caller: StartAuthority) -> None:
        if caller is not self._start_authority:
            raise UnauthorizedStart("caller lacks launcher authority")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(_connect(self._path, mode="rw")) as conn:
                conn.execute("PRAGMA synchronous=EXTRA")
                conn.execute("BEGIN IMMEDIATE")
                if (conn.execute("PRAGMA user_version").fetchone() != (_FORMAT_VERSION,)
                        or conn.execute("PRAGMA quick_check(1)").fetchone() != ("ok",)):
                    raise LedgerUnavailable("unknown launcher ledger format or integrity")
                yield conn
                conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise LedgerUnavailable("could not access or commit the launcher ledger") from exc

    def start_or_get(
        self, caller: StartAuthority, request: StartRequest, *, allow_retry: bool = False,
    ) -> Reservation:
        """Return stable IDs and dispatch only a proven never-started reservation.

        A new numbered retry needs a released predecessor and explicit trusted
        permission. Replaying a known key must still match its original inputs.
        """
        self._authorize(caller)
        fingerprint = _fingerprint(request)
        if type(allow_retry) is not bool:
            raise ValueError("expected explicit retry policy")
        record = self._reserve(request.key, fingerprint, allow_retry)
        reservation = record.reservation
        if record.state is AttemptState.RESERVED and self._claim_dispatch(reservation):
            with self._transaction() as conn:
                # Cancellation may have won since the durable dispatch claim.
                active = _active(conn)
                if (active is not None and active.reservation == reservation
                        and active.state is AttemptState.UNKNOWN):
                    self._manager.start(reservation, request)
        return reservation

    def _reserve(self, key: AttemptKey, fingerprint: str, allow_retry: bool) -> _Record:
        with self._transaction() as conn:
            active = _active(conn)
            if active is not None and _matches(active, key):
                existing = active
            else:
                existing = _completed(conn, key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise ConflictingRequest("different inputs for an existing attempt key")
                return existing

            if active is not None and self._settle(conn, active).slot_reserved:
                error = SlotOccupied("the worker slot is reserved")
            elif key.attempt > 1 and (not allow_retry or _completed(
                    conn, replace(key, attempt=key.attempt - 1)) is None):
                error = RetryNotAllowed("retry needs a reconciled predecessor and explicit permission")
            else:
                run_id = str(uuid4())
                reservation = Reservation(key, run_id, _unit_id(run_id), str(uuid4()))
                record = _Record(reservation, fingerprint)
                _set_active(conn, record)
                return record
        # Keep reconciliation progress even if the new admission was refused.
        raise error

    def _claim_dispatch(self, reservation: Reservation) -> bool:
        with self._transaction() as conn:
            active = _active(conn)
            if (active is None or active.reservation != reservation
                    or active.state is not AttemptState.RESERVED):
                return False
            # Once committed, this claim makes even a lost receipt or a missing
            # unit ambiguous. Only its original claimant may call start.
            _set_active(conn, replace(active, state=AttemptState.UNKNOWN))
            return True

    def _inspect(self, reservation: Reservation) -> WorkerState | None:
        try:
            return self._manager.inspect(reservation)
        except WorkerUnavailable:
            return None

    def _settle(self, conn: sqlite3.Connection, record: _Record, *, stop: bool = False) -> AttemptStatus:
        reservation, state = record.reservation, record.state
        if state is AttemptState.UNKNOWN or state is AttemptState.CANCELLING:
            worker = self._inspect(reservation)
            if state is AttemptState.CANCELLING and stop and worker is WorkerState.RUNNING:
                try:
                    self._manager.stop(reservation)
                except WorkerUnavailable:
                    return AttemptStatus(reservation, state, slot_reserved=True)
                worker = self._inspect(reservation)
            if worker is WorkerState.SUCCEEDED:
                state = AttemptState.SUCCEEDED
            elif worker is WorkerState.FAILED:
                state = AttemptState.FAILED
            elif worker is WorkerState.CLEAN and state is AttemptState.CANCELLING:
                state = AttemptState.CANCELLED
            elif worker is WorkerState.RUNNING and state is AttemptState.UNKNOWN:
                return AttemptStatus(reservation, AttemptState.RUNNING, slot_reserved=True)

        if state in _TERMINAL:
            if state is not record.state:
                record = replace(record, state=state)
                _set_active(conn, record)
            try:
                effects = self._manager.effects_reconciled(reservation)
            except WorkerUnavailable:
                effects = None
            if effects is True:
                return _release(conn, record)
        return AttemptStatus(reservation, state, slot_reserved=True)

    def status(self, caller: StartAuthority, target: AttemptKey | str) -> AttemptStatus:
        """Refresh status by key or run ID without starting or stopping anything."""
        self._authorize(caller)
        return self._status(target)

    def _status(self, target: AttemptKey | str, *, stop: bool = False) -> AttemptStatus:
        if not (_valid_key(target) or _valid_id(target)):
            raise ValueError("expected an attempt key or run ID")
        with self._transaction() as conn:
            active = _active(conn)
            if active is not None and _matches(active, target):
                return self._settle(conn, active, stop=stop)
            completed = _completed(conn, target)
            if completed is None:
                raise AttemptNotFound("no record for this attempt")
            return AttemptStatus(completed.reservation, completed.state, slot_reserved=False)

    def cancel(self, caller: StartAuthority, target: AttemptKey | str) -> AttemptStatus:
        """Stop only the owned run; never roll back external effects."""
        self._authorize(caller)
        if not (_valid_key(target) or _valid_id(target)):
            raise ValueError("expected an attempt key or run ID")
        with self._transaction() as conn:
            active = _active(conn)
            if active is not None and _matches(active, target):
                if active.state is AttemptState.RESERVED:
                    # No dispatch claim (and thus no possible external effect).
                    return _release(conn, replace(active, state=AttemptState.CANCELLED))
                if active.state is AttemptState.UNKNOWN:
                    worker = self._inspect(active.reservation)
                    if (not isinstance(worker, WorkerState)
                            or worker in (WorkerState.FOREIGN, WorkerState.MISSING)):
                        return AttemptStatus(active.reservation, AttemptState.UNKNOWN, slot_reserved=True)
                    # Commit intent before stop so a later lookup can finish
                    # reconciliation even after an interrupted cancellation.
                    _set_active(conn, replace(active, state=AttemptState.CANCELLING))
        return self._status(target, stop=True)
