"""Issue #48 validation gate for supervised, operator-pinned fixtures only."""

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

from . import live_e2e
from .feature_tree import EvidenceReadError, IssueRef, valid_issue_ref
from .readiness import valid_sha


@dataclass(frozen=True)
class Binding:
    pr: IssueRef
    head_sha: str
    base_sha: str
    merge_sha: str
    adr_revision: str


def _valid_binding(binding: object) -> bool:
    return (isinstance(binding, Binding) and valid_issue_ref(binding.pr)
            and all(valid_sha(sha) for sha in (binding.head_sha, binding.base_sha,
                                               binding.merge_sha, binding.adr_revision)))


@dataclass(frozen=True)
class Check:
    binding: Binding
    producer: str  # authenticated producer identity from the trusted reader, NOT a displayed check name
    run_id: str
    status: str


class Disposition(Enum):
    FIXED = 'fixed'
    EXPLAINED = 'explained'


@dataclass(frozen=True)
class Review:
    check: Check
    findings: tuple[Disposition, ...] | None  # () means a complete review with no findings


@dataclass(frozen=True)
class Evidence:
    binding: Binding | None
    open_nondraft: bool | None
    redesign_pending: bool | None
    ci: Check | None
    review: Review | None


@dataclass(frozen=True)
class Fixture:
    """Trusted operator config, NOT parameters from a candidate, PR or worker."""

    binding: Binding
    repo: Path  # protected Git object source, independent of the staged checkout
    checkout: Path
    python: Path  # separately provisioned fixture environment
    ci_producer: str
    review_producer: str


@dataclass(frozen=True)
class FixtureAttestation:
    binding: Binding
    ci_run: str
    review_run: str

    def matches(self, expected: Binding) -> bool:
        """Compare fixture identities only; this does not authorize a story merge."""
        return _valid_binding(expected) and self.binding == expected


def _passed(check: object, binding: Binding, producer: str) -> bool:
    return (isinstance(check, Check) and check.binding == binding and check.producer == producer
            and isinstance(check.run_id, str) and bool(check.run_id.strip()) and check.status == 'success')


def _eligible(fixture: Fixture, read: Callable[[IssueRef], Evidence | None]) -> Evidence:
    try:
        evidence = read(fixture.binding.pr)
    except (EvidenceReadError, OSError) as exc:
        raise live_e2e.Blocked('could not read current CI and review evidence') from exc
    if (not isinstance(evidence, Evidence) or evidence.binding != fixture.binding
            or evidence.open_nondraft is not True or evidence.redesign_pending is not False):
        raise live_e2e.Blocked('candidate is unknown, ineligible or blocked by redesign')
    if not _passed(evidence.ci, fixture.binding, fixture.ci_producer):
        raise live_e2e.Blocked('no matching trusted CI success')
    review = evidence.review
    if (not isinstance(review, Review) or not _passed(review.check, fixture.binding, fixture.review_producer)
            or not isinstance(review.findings, tuple)
            or not all(isinstance(item, Disposition) for item in review.findings)):
        raise live_e2e.Blocked('no matching trusted PR-Agent success with all findings resolved')
    return evidence


@dataclass(frozen=True)
class _TreeEntry:
    mode: bytes
    oid: bytes


def _git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ['/usr/bin/git', '-C', repo, *args], timeout=30, stderr=subprocess.DEVNULL,
            env={'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                 'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_NO_LAZY_FETCH': '1'},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise live_e2e.Blocked('cannot inspect pinned fixture commit') from exc


def _tree(repo: Path, sha: str) -> dict[Path, _TreeEntry]:
    if _git(repo, 'rev-parse', '--verify', f'{sha}^{{commit}}').strip() != sha.encode('ascii'):
        raise live_e2e.Blocked('fixture pin is not a commit')
    records = _git(repo, 'ls-tree', '-r', '-t', '-z', sha).split(b'\0')
    entries = {}
    for record in records[:-1]:
        header, name = record.split(b'\t', 1)
        mode, _, oid = header.split(b' ')
        path = Path(os.fsdecode(name))
        if mode not in {b'040000', b'100644', b'100755'} or path in entries:
            raise live_e2e.Blocked('unsupported entry in pinned fixture tree')
        entries[path] = _TreeEntry(mode, oid)
    return entries


def _check_entry(dirfd: int, name: str, entry: _TreeEntry) -> None:
    info = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    if entry.mode == b'040000':
        if not stat.S_ISDIR(info.st_mode):
            raise live_e2e.Blocked('fixture directory does not match the pinned tree')
        return
    actual_mode = b'100755' if info.st_mode & stat.S_IXUSR else b'100644'
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or actual_mode != entry.mode:
        raise live_e2e.Blocked('fixture file type or mode does not match the pinned tree')
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dirfd)
    with os.fdopen(fd, 'rb') as stream:
        if not os.path.samestat(info, os.fstat(fd)):
            raise live_e2e.Blocked('fixture file was replaced during verification')
        digest = hashlib.sha1(f'blob {info.st_size}\0'.encode('ascii'), usedforsecurity=False)
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest().encode('ascii') != entry.oid:
        raise live_e2e.Blocked('fixture file bytes do not match the pinned tree')


def _walk_error(error: OSError) -> None:
    raise error


def _verify_checkout(checkout: Path, fd: int, tree: dict[Path, _TreeEntry]) -> None:
    if not os.path.samestat(checkout.lstat(), os.fstat(fd)):
        raise live_e2e.Blocked('fixture checkout was replaced')
    remaining = tree.copy()
    for root, dirs, files, dirfd in os.fwalk(dir_fd=fd, onerror=_walk_error):
        for name in dirs + files:
            entry = remaining.pop(Path(root) / name, None)
            if entry is None:
                raise live_e2e.Blocked('unexpected entry in fixture checkout')
            _check_entry(dirfd, name, entry)
    if remaining:
        raise live_e2e.Blocked('fixture checkout is missing pinned entries')


def _write_receipt(path: Path, result: FixtureAttestation) -> None:
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump({'kind': 'supervised-fixture-e2e', **asdict(result)}, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, path)  # publish complete bytes; never replace an existing receipt
        finally:
            temporary.unlink()


def validate_fixture(
    target: live_e2e.Target, fixture: Fixture, read: Callable[[IssueRef], Evidence | None], receipt: Path,
) -> FixtureAttestation:
    """Run one pinned fixture through the lease, or fail with no new receipt.

    All arguments, including the reader itself, come from the trusted supervisor.
    The reader authenticates producers, verifies completeness/currentness,
    and normalizes facts; PR text, check names and local JSON are not such a reader.
    The repo, staged tree, interpreter, supervisor and receipt directory must be
    protected from candidate writes. This is not a same-UID isolation boundary.
    """
    if (not _valid_binding(fixture.binding)
            or not all(path.is_absolute() for path in (fixture.repo, fixture.checkout, fixture.python, receipt))
            or not all(isinstance(p, str) and p.strip() for p in (fixture.ci_producer, fixture.review_producer))
            or fixture.ci_producer == fixture.review_producer):
        raise live_e2e.Blocked('valid pinned fixture configuration required')
    with live_e2e._locked(target) as lease:
        if os.path.lexists(receipt):
            raise live_e2e.Blocked('fixture receipt already exists')
        evidence = _eligible(fixture, read)
        tree = _tree(fixture.repo, fixture.binding.merge_sha)
        fd = os.open(fixture.checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _verify_checkout(fixture.checkout, fd, tree)

            def suite(lease: int) -> int:
                if _eligible(fixture, read) != evidence:
                    raise live_e2e.Blocked('CI or review evidence changed before launch')
                _verify_checkout(fixture.checkout, fd, tree)
                return live_e2e._run_suite(target, fixture.checkout, fixture.python, lease, None, checkout_fd=fd)

            if live_e2e._run_locked(target, lease, suite) != 0:
                raise live_e2e.Blocked('fixture suite did not pass; inspect and recover before reuse')
            if _eligible(fixture, read) != evidence:
                raise live_e2e.Blocked('CI or review evidence changed during validation')
            _verify_checkout(fixture.checkout, fd, tree)
        finally:
            os.close(fd)
        result = FixtureAttestation(fixture.binding, evidence.ci.run_id, evidence.review.check.run_id)
        _write_receipt(receipt, result)
        return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='operation', required=True)
    demo = modes.add_parser('fixture', help='run an authorized fixture with synthetic CI and review')
    for name in ('repo', 'checkout', 'python', 'receipt'):
        demo.add_argument(f'--{name}', required=True, type=Path)
    demo.add_argument('--pin', required=True, help='reviewed fixture commit (full SHA)')
    args = parser.parse_args(argv)
    binding = Binding(IssueRef('fixture/floe', 1), '1' * 40, '2' * 40, args.pin, '3' * 40)
    fixture = Fixture(binding, args.repo, args.checkout, args.python, 'fixture-ci', 'fixture-review')
    ci = Check(binding, fixture.ci_producer, 'synthetic-ci', 'success')
    review = Review(Check(binding, fixture.review_producer, 'synthetic-review', 'success'), ())
    try:
        validate_fixture(live_e2e.Target.local(), fixture,
                         lambda _: Evidence(binding, True, False, ci, review), args.receipt)
    except (live_e2e.Blocked, OSError) as exc:
        print(f'checked E2E blocked: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
