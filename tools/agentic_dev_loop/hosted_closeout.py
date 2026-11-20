"""Independent closeout check: inspect Git objects, never run a candidate checkout."""

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .closeout_policy import (
    _FILES, ReleaseStatus, SealedCloseout, SealedFile, _content_status, _destinations, delta_digest,
)
from .feature_tree import IssueRef
from .handoffs import _feature_tag
from .readiness import valid_sha


@dataclass(frozen=True, slots=True)
class Commit:
    base_sha: str
    head_sha: str
    delta_sha256: str


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ['/usr/bin/git', '-C', repo, *args], timeout=15, stderr=subprocess.DEVNULL,
        env={'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
             'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_NO_LAZY_FETCH': '1'},
    )


def _read_delta(repo: Path, base: str, head: str, tag: str) -> SealedCloseout:
    if (not repo.is_absolute() or not valid_sha(base) or not valid_sha(head) or base == head
            or _git(repo, 'rev-parse', '--verify', f'{base}^{{commit}}').strip() != base.encode()
            or _git(repo, 'rev-list', '--parents', '--max-count=1', head).split() != [head.encode(), base.encode()]):
        raise ValueError
    destinations = _destinations(tag)
    expected = {str(path).encode(): limit for path, (_, limit) in zip(destinations, _FILES, strict=True)}
    parents = {str(parent).encode() for parent in destinations[0].parents if parent.parts}
    # Include tree changes, so an extra empty subtree is not invisible.
    raw = _git(repo, 'diff-tree', '--no-commit-id', '-r', '-t', '--raw', '--no-renames', '--no-abbrev',
               '--no-relative', '--no-ext-diff', '--no-textconv', '--ignore-submodules=none',
               '-z', base, head).split(b'\0')
    if raw[-1] or len(raw) % 2 != 1:
        raise ValueError
    blobs = {}
    seen = set()
    for header, path in zip(raw[:-1:2], raw[1::2], strict=True):
        if path in seen:
            raise ValueError
        seen.add(path)
        old_mode, mode, _, oid, change = header.split()
        if path in parents and (old_mode, mode, change) in {
            (b':000000', b'040000', b'A'), (b':040000', b'040000', b'M'),
        }:
            continue
        if path not in expected or (old_mode, mode, change) != (b':000000', b'100644', b'A'):
            raise ValueError
        object_id = oid.decode('ascii')
        if not valid_sha(object_id):
            raise ValueError
        blobs[path] = object_id
    if blobs.keys() != expected.keys():
        raise ValueError
    files = []
    for path in destinations:
        name = str(path).encode()
        oid = blobs[name]
        if (_git(repo, 'cat-file', '-t', oid).strip() != b'blob'
                or not 0 < int(_git(repo, 'cat-file', '-s', oid)) <= expected[name]):
            raise ValueError
        files.append(SealedFile(path, _git(repo, 'cat-file', 'blob', oid)))
    return SealedCloseout(tuple(files))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path, help='protected Git object store')
    parser.add_argument('--repository', required=True, help='expected owner/name from trusted policy')
    parser.add_argument('--feature', required=True, type=int)
    parser.add_argument('--slug', required=True)
    parser.add_argument('--base', required=True, help='exact current master SHA')
    parser.add_argument('--head', required=True, help='exact closeout commit SHA')
    parser.add_argument('--scanner', required=True, type=Path, help='absolute path to the pinned scanner')
    args = parser.parse_args(argv)
    feature = IssueRef(args.repository, args.feature)
    try:
        tag = _feature_tag(feature, args.slug)
        sealed = _read_delta(args.repo, args.base, args.head, tag)
    except (OSError, ValueError, subprocess.SubprocessError):
        status = ReleaseStatus.INVALID_CANDIDATE
    else:
        status = _content_status(tuple(file.data for file in sealed.files), feature, tag, args.scanner)
    if status is not None:
        print(status, file=sys.stderr)
        return 1
    print(json.dumps(asdict(Commit(args.base, args.head, delta_digest(sealed))), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
