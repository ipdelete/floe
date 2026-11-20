"""Trusted reconciliation of role writes over normalized GitHub evidence.

This is a fixture boundary, not a live adapter or an authorization interface.
Use one protected local journal per repository for every cooperating writer.
"""

import fcntl
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Protocol

from .launcher import AttemptKey, AttemptState, Reservation
from .readiness import Role


@dataclass(frozen=True, slots=True)
class Push:
    ref: str
    old_sha: str | None
    sha: str


@dataclass(frozen=True, slots=True)
class CreateIssue:
    marker: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class LinkSubIssue:
    parent_id: int
    child_id: int


@dataclass(frozen=True, slots=True)
class CloseIssue:
    issue_id: int


@dataclass(frozen=True, slots=True)
class CreatePR:
    base: str
    head: str
    base_sha: str
    head_sha: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class RunRecord:
    comment_id: int
    reservation: Reservation
    status: AttemptState


@dataclass(frozen=True, slots=True)
class WriteRun:
    reservation: Reservation
    previous: RunRecord | None
    status: AttemptState


type Change = Push | CreateIssue | LinkSubIssue | CloseIssue | CreatePR | WriteRun


@dataclass(frozen=True, slots=True)
class Operation:
    id: str
    repo_id: int
    change: Change


@dataclass(frozen=True, slots=True)
class RefState:
    ref: str
    sha: str | None


@dataclass(frozen=True, slots=True)
class IssueRecord:
    issue_id: int
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class IssuesState:
    marker: str
    matches: tuple[IssueRecord, ...]


@dataclass(frozen=True, slots=True)
class ParentState:
    parent_id: int  # requested parent, verified to exist
    child_id: int  # verified to exist
    current_parent_id: int | None


@dataclass(frozen=True, slots=True)
class IssueState:
    issue_id: int  # verified to exist
    closed: bool


@dataclass(frozen=True, slots=True)
class PRRecord:
    number: int
    head_sha: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class PRState:
    base: str
    head: str
    base_sha: str
    head_sha: str
    matches: tuple[PRRecord, ...]


@dataclass(frozen=True, slots=True)
class RunState:
    key: AttemptKey
    current: RunRecord | None


type Snapshot = RefState | IssuesState | ParentState | IssueState | PRState | RunState


@dataclass(frozen=True, slots=True)
class NonApplication:
    """Positive proof this exact send settled without applying and cannot commit later."""

    operation_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class Evidence:
    repo_id: int
    snapshot: Snapshot
    pending: bool = False
    nonapplication: NonApplication | None = None


class TransportUnavailable(Exception):
    """The transport cannot verify a read, or cannot account for a send."""


class Transport(Protocol):
    """Trusted, deadline-bounded adapter. Never retry internally.

    Return fresh evidence for the actual repository and stable identities, not
    echoes of the request. Empty/None means verified absence, not missing data.
    Issue-marker and PR branch-pair searches cover all matches (including closed
    records); PRs must use the exact same-repository base and head. A run read
    folds complete trusted append-only notes by attempt, retaining terminal
    status; inconsistent, duplicate or incomplete note history fails the read.
    Raise TransportUnavailable for unsupported or incomplete reads.
    """

    def read(self, operation: Operation, *, deadline: float) -> Evidence: ...

    def send(self, operation: Operation, *, generation: int, deadline: float) -> None:
        """Send once in the expected repository; use an exact old-ref lease for push.

        Append run notes; never edit or delete one. A sub-issue link must not
        reparent an existing child. The logical writer serializes this service's
        calls, not external actors. For the other endpoints, a preflight read
        does not give server-side compare-and-swap. A response is not evidence.
        """
        ...


class Outcome(StrEnum):
    VERIFIED = "verified"
    CONFLICT = "conflict"
    IN_FLIGHT = "in-flight"
    UNKNOWN = "unknown"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class Result:
    outcome: Outcome
    # The exact request is journaled by fingerprint; receipt gives the remote
    # ref SHA, created issue ID, linked/closed issue ID, PR number or comment ID.
    receipt: int | str | None = None


_TERMINAL = frozenset({AttemptState.SUCCEEDED, AttemptState.FAILED, AttemptState.CANCELLED})
_RUN_STATES = _TERMINAL | {AttemptState.RESERVED}


def _id(value: object) -> bool:
    return type(value) is int and value > 0


def _text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _sha(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _key(value: object) -> bool:
    return (isinstance(value, AttemptKey) and isinstance(value.role, Role)
            and all(_id(i) for i in (value.repo_id, value.feature_id, value.issue_id, value.attempt)))


def _reservation(value: object) -> bool:
    return (isinstance(value, Reservation) and _key(value.key)
            and all(_text(v) for v in (value.run_id, value.unit_id, value.session_id)))


def _run_record(value: object) -> bool:
    return (isinstance(value, RunRecord) and _id(value.comment_id) and _reservation(value.reservation)
            and isinstance(value.status, AttemptState) and value.status in _RUN_STATES)


def _target(operation: Operation) -> tuple:
    """Validate the immutable request and select its journal contention key."""
    change = operation.change
    match change:
        case Push(ref, old, sha) if (_text(ref) and ref.startswith("refs/heads/")
                                    and (old is None or _sha(old)) and _sha(sha) and sha != old):
            return ("ref", ref)
        case CreateIssue(marker, title, body) if _text(marker) and _text(title) and type(body) is str:
            return ("issue-marker", marker)
        case LinkSubIssue(parent, child) if _id(parent) and _id(child) and parent != child:
            return ("parent-of", child)
        case CloseIssue(issue) if _id(issue):
            return ("close-issue", issue)
        case CreatePR(base, head, base_sha, head_sha, title, body) if (
            _text(base) and _text(head) and _sha(base_sha) and _sha(head_sha)
            and _text(title) and type(body) is str
        ):
            return ("pr", base, head)
        case WriteRun(reservation, previous, status) if (
            _reservation(reservation) and reservation.key.repo_id == operation.repo_id
            and isinstance(status, AttemptState) and status in _RUN_STATES
            and (previous is None or (status in _TERMINAL and _run_record(previous)
                 and previous.reservation == reservation and previous.status is AttemptState.RESERVED))
        ):
            key = reservation.key
            return ("run", key.feature_id, key.issue_id, key.role, key.attempt)
    raise ValueError("invalid GitHub write request")


class _Gate(StrEnum):
    ABSENT = "absent"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


def _compare(change: Change, snapshot: Snapshot) -> int | str | _Gate:
    """Return an exact receipt, or compare the original preconditions."""
    match change, snapshot:
        case Push(), RefState(ref, sha):
            if sha is not None and not _sha(sha):
                return _Gate.UNKNOWN
            if ref == change.ref:
                if sha == change.sha:
                    return sha
                if sha == change.old_sha:
                    return _Gate.ABSENT
        case CreateIssue(), IssuesState(marker, matches):
            if not isinstance(matches, tuple) or not all(
                isinstance(item, IssueRecord) and _id(item.issue_id) and _text(item.title)
                and type(item.body) is str for item in matches
            ):
                return _Gate.UNKNOWN
            if marker == change.marker:
                if not matches:
                    return _Gate.ABSENT
                if len(matches) == 1 and (matches[0].title, matches[0].body) == (change.title, change.body):
                    return matches[0].issue_id
        case LinkSubIssue(), ParentState(parent, child, current):
            if not (_id(parent) and _id(child) and (current is None or _id(current))):
                return _Gate.UNKNOWN
            if (parent, child) == (change.parent_id, change.child_id):
                if current == parent:
                    return child
                if current is None:
                    return _Gate.ABSENT
        case CloseIssue(), IssueState(issue, closed):
            if not _id(issue) or type(closed) is not bool:
                return _Gate.UNKNOWN
            if issue == change.issue_id:
                return issue if closed else _Gate.ABSENT
        case CreatePR(), PRState(base, head, base_sha, head_sha, matches):
            if not (_sha(base_sha) and _sha(head_sha) and isinstance(matches, tuple)) or not all(
                isinstance(item, PRRecord) and _id(item.number) and _sha(item.head_sha)
                and _text(item.title) and type(item.body) is str for item in matches
            ):
                return _Gate.UNKNOWN
            if (base, head, base_sha, head_sha) == (change.base, change.head, change.base_sha, change.head_sha):
                if not matches:
                    return _Gate.ABSENT
                if len(matches) == 1 and (matches[0].head_sha, matches[0].title, matches[0].body) == (
                    change.head_sha, change.title, change.body
                ):
                    return matches[0].number
        case WriteRun(), RunState(key, current):
            if not _key(key) or (current is not None and (
                not _run_record(current) or current.reservation.key != key
            )):
                return _Gate.UNKNOWN
            if key == change.reservation.key:
                if current is not None and current.reservation == change.reservation and current.status is change.status:
                    return current.comment_id
                if current == change.previous:
                    return _Gate.ABSENT
        case _:
            return _Gate.UNKNOWN
    return _Gate.CONFLICT


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _open(directory: Path, *, mode: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{(directory / 'writes.sqlite3').as_uri()}?mode={mode}",
                           uri=True, autocommit=True, timeout=0)
    try:
        conn.execute("PRAGMA synchronous=EXTRA")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def initialize_writer(directory: Path) -> None:
    """Provision a fresh private journal, never replace an existing one."""
    directory = directory.absolute()
    directory.mkdir(mode=0o700)
    with closing(_open(directory, mode="rwc")) as conn:
        conn.executescript("""BEGIN;
            CREATE TABLE writes (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, target TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0, receipt ANY
            ) STRICT;
            CREATE INDEX writes_target ON writes(target);
            CREATE TABLE terminal_run (target TEXT PRIMARY KEY, fingerprint TEXT NOT NULL) STRICT;
            COMMIT;
        """)
    (directory / "writer.lock").touch(mode=0o600, exist_ok=False)


class Writer:
    def __init__(self, directory: Path, repo_id: int, transport: Transport) -> None:
        if not _id(repo_id):
            raise ValueError("expected a stable repository ID")
        self._directory = directory.absolute()
        self._repo_id = repo_id
        self._transport = transport

    def reconcile(self, operation: Operation, *, deadline: float) -> Result:
        """At most one send per call. Reopen this same journal after interruption."""
        if (not isinstance(operation, Operation) or not _text(operation.id) or not _id(operation.repo_id)
                or type(deadline) not in (int, float) or not isfinite(deadline)):
            raise ValueError("expected an operation ID, repository and finite monotonic deadline")
        if operation.repo_id != self._repo_id:
            return Result(Outcome.CONFLICT)
        target = json.dumps((operation.repo_id, *_target(operation)))
        fingerprint = _digest([type(operation.change).__name__, asdict(operation)])
        try:
            # Keep the writer lock across both durable commits and remote calls.
            # In particular, another process can't see a claim and send before us.
            with (self._directory / "writer.lock").open("rb") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return Result(Outcome.BUSY)
                with closing(_open(self._directory, mode="rw")) as conn:
                    return self._reconcile(conn, operation, target, fingerprint, deadline)
        except (OSError, sqlite3.Error):
            return Result(Outcome.UNKNOWN)

    def _read(
        self, conn: sqlite3.Connection, operation: Operation, target: str, deadline: float,
    ) -> Evidence | Result:
        if monotonic() >= deadline:
            return Result(Outcome.UNKNOWN)
        try:
            evidence = self._transport.read(operation, deadline=deadline)
        except (OSError, TransportUnavailable):
            return Result(Outcome.UNKNOWN)
        if (not isinstance(evidence, Evidence) or not _id(evidence.repo_id)
                or type(evidence.pending) is not bool or monotonic() >= deadline):
            return Result(Outcome.UNKNOWN)
        if evidence.repo_id != operation.repo_id:
            return Result(Outcome.CONFLICT)
        gate = _compare(operation.change, evidence.snapshot)
        if gate is _Gate.UNKNOWN:
            return Result(Outcome.UNKNOWN)
        if evidence.pending:
            return Result(Outcome.IN_FLIGHT)
        if isinstance(operation.change, WriteRun) and isinstance(evidence.snapshot, RunState):
            current = evidence.snapshot.current
            terminal = None
            if (evidence.snapshot.key == operation.change.reservation.key and current is not None
                    and current.status in _TERMINAL):
                terminal = _digest(asdict(current))
                conn.execute("INSERT OR IGNORE INTO terminal_run VALUES (?, ?)", (target, terminal))
            prior = conn.execute("SELECT fingerprint FROM terminal_run WHERE target = ?", (target,)).fetchone()
            if prior is not None and prior[0] != terminal:
                return Result(Outcome.CONFLICT)
        if gate is _Gate.CONFLICT:
            return Result(Outcome.CONFLICT)
        if gate is _Gate.ABSENT:
            return evidence
        conn.execute("UPDATE writes SET receipt = ? WHERE id = ?", (gate, operation.id))
        return Result(Outcome.VERIFIED, gate)

    def _reconcile(
        self, conn: sqlite3.Connection, operation: Operation, target: str, fingerprint: str, deadline: float,
    ) -> Result:
        row = conn.execute("SELECT fingerprint, generation, receipt FROM writes WHERE id = ?",
                           (operation.id,)).fetchone()
        generation = 0
        if row is None:
            conn.execute("INSERT INTO writes (id, fingerprint, target) VALUES (?, ?, ?)",
                         (operation.id, fingerprint, target))
        else:
            stored, generation, receipt = row
            if stored != fingerprint:
                return Result(Outcome.CONFLICT)
            if receipt is not None:
                return Result(Outcome.VERIFIED, receipt)
        if conn.execute("""SELECT 1 FROM writes WHERE target = ? AND id != ?
                            AND generation > 0 AND receipt IS NULL LIMIT 1""",
                        (target, operation.id)).fetchone():
            return Result(Outcome.IN_FLIGHT)

        evidence = self._read(conn, operation, target, deadline)
        if isinstance(evidence, Result):
            return evidence
        # Only push can use positive nonapplication evidence at this boundary.
        # Neither an unchanged ref nor a stale proof from an older generation suffices.
        proof = evidence.nonapplication
        if generation and not (isinstance(operation.change, Push) and isinstance(proof, NonApplication)
                               and _id(proof.generation) and proof == NonApplication(operation.id, generation)):
            return Result(Outcome.IN_FLIGHT)
        if monotonic() >= deadline:
            return Result(Outcome.UNKNOWN)
        generation += 1
        conn.execute("UPDATE writes SET generation = ? WHERE id = ?", (generation, operation.id))
        if monotonic() >= deadline:
            return Result(Outcome.UNKNOWN)
        try:
            self._transport.send(operation, generation=generation, deadline=deadline)
        except (OSError, TransportUnavailable):
            pass  # Re-read even on lost reply, never send a second time in this call.
        result = self._read(conn, operation, target, deadline)
        return result if isinstance(result, Result) else Result(Outcome.IN_FLIGHT)
