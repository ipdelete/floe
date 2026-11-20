"""Trusted admission for fixture workers, with one durable slot per ledger.

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
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
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


class StartAuthority:
    """Identity capability held by the trusted caller, not worker input."""

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


_LEDGER_NAME = "ledger.sqlite3"
_FORMAT_VERSION = 1


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


def _decode(record: str) -> tuple[Reservation, str]:
    try:
        fields = json.loads(record)
        if not isinstance(fields, dict):
            raise ValueError("expected a record")
        fingerprint = fields.pop("fingerprint")
        key = fields.pop("key")
        key["role"] = Role(key["role"])
        reservation = Reservation(key=AttemptKey(**key), **fields)
        if (not _valid_key(reservation.key) or not _hex(fingerprint, 64)
                or not all(isinstance(value, str) and str(UUID(value)) == value
                           for value in (reservation.run_id, reservation.session_id))
                or reservation.unit_id != _unit_id(reservation.run_id)):
            raise ValueError("invalid record")
        return reservation, fingerprint
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise LedgerUnavailable("invalid launcher ledger record") from exc


class Launcher:
    def __init__(
        self, state_dir: Path, start_authority: StartAuthority,
        start_worker: Callable[[Reservation, StartRequest], None],
    ) -> None:
        if not isinstance(start_authority, StartAuthority):
            raise ValueError("expected a trusted start capability")
        self._path = state_dir.absolute() / _LEDGER_NAME
        self._start_authority = start_authority
        self._start_worker = start_worker

    def start_or_get(self, caller: StartAuthority, request: StartRequest) -> Reservation:
        """Reserve once, then call the adapter only for a newly committed record.

        A replay returns the reservation even if the original adapter call is
        still in flight or raised. There is deliberately no release or recovery
        operation in this slice.
        """
        if caller is not self._start_authority:
            raise UnauthorizedStart("caller lacks start authority")
        fingerprint = _fingerprint(request)
        try:
            with closing(_connect(self._path, mode="rw")) as conn:
                conn.execute("PRAGMA synchronous=EXTRA")
                conn.execute("BEGIN IMMEDIATE")
                if (conn.execute("PRAGMA user_version").fetchone() != (_FORMAT_VERSION,)
                        or conn.execute("PRAGMA quick_check(1)").fetchone() != ("ok",)):
                    raise LedgerUnavailable("unknown launcher ledger format or integrity")
                row = conn.execute("SELECT record FROM slot WHERE id = 1").fetchone()
                if row is None:
                    raise LedgerUnavailable("launcher slot record is missing")
                [record] = row
                if record is not None:
                    reservation, expected = _decode(record)
                    if reservation.key != request.key:
                        raise SlotOccupied("the worker slot is reserved")
                    if expected != fingerprint:
                        raise ConflictingRequest("different inputs for an existing attempt key")
                    return reservation

                run_id = str(uuid4())
                reservation = Reservation(request.key, run_id, _unit_id(run_id), str(uuid4()))
                record = json.dumps({**asdict(reservation), "fingerprint": fingerprint})
                updated = conn.execute("UPDATE slot SET record = ? WHERE id = 1", (record,))
                if updated.rowcount != 1:
                    raise LedgerUnavailable("launcher reservation was not written")
                conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise LedgerUnavailable("could not access or commit the launcher ledger") from exc

        self._start_worker(reservation, request)
        return reservation
