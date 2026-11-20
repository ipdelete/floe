"""Exact-head PR-Agent review policy and a read-only GitHub check."""

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
import subprocess
import sys

from .feature_tree import IssueRef, valid_issue_ref
from .readiness import valid_sha


_STATE_NAMESPACE = '<!-- pr-agent-review-state'
_STATE = re.compile(r'<!-- pr-agent-review-state:v1\n(.*?)\n-->\s*\Z', re.DOTALL)
_FULL_MARKER = '<!-- pr-agent:review:full -->'
_STANDALONE_HEADER = '## Standalone PR Review'
_FINDING_ID = re.compile(r'[a-zA-Z0-9_-]+', re.ASCII)
_DISPOSITION = re.compile(r'- ([a-zA-Z0-9_-]+): (?:fixed|not applicable) — (\S.*)')
_GH_NAME = re.compile(r'[a-zA-Z0-9_.-]+')


class ReviewBlocked(Exception):
    """The available evidence cannot clear the review gate."""


@dataclass(frozen=True)
class Comment:
    id: int
    author: str  # authenticated login from comment metadata, not the comment body
    created_at: datetime
    updated_at: datetime
    body: str


@dataclass(frozen=True)
class StandaloneRun:
    """Trusted runner evidence for a standalone publication, not a PR assertion.

    Standalone markdown omits the head, completeness and stable finding IDs;
    it may also omit inline findings. The runner must independently know all
    findings (including inline ones) for this exact published comment version.
    """

    comment: Comment
    head_sha: str
    complete: bool
    finding_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReviewEvidence:
    """Complete current comment inventory from a trusted reader for one PR."""

    pr: IssueRef
    comments: tuple[Comment, ...]
    standalone: StandaloneRun | None = None


@dataclass(frozen=True)
class ReviewRun:
    run_id: str
    active_ids: tuple[str, ...]


@dataclass(frozen=True)
class _PersistentRun:
    head_sha: str
    complete: bool
    active_ids: tuple[str, ...]


def _finding_id(value: object) -> bool:
    return isinstance(value, str) and _FINDING_ID.fullmatch(value) is not None


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _persistent(comment: Comment) -> _PersistentRun:
    malformed = ReviewBlocked('malformed PR-Agent persistent review state')
    match = _STATE.search(comment.body)
    if match is None or comment.body.count(_STATE_NAMESPACE) != 1:
        raise malformed
    try:
        state = json.loads(match[1], object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        raise malformed from None
    if (not isinstance(state, dict) or type(state.get('schema_version')) is not int
            or state['schema_version'] != 1):
        raise malformed
    run, findings = state.get('last_run'), state.get('findings')
    if (not isinstance(run, dict) or not valid_sha(run.get('head_sha'))
            or type(run.get('complete')) is not bool
            or run.get('kind') != ('full' if run['complete'] else 'partial')
            or not isinstance(run.get('run_id'), str) or not run['run_id'].strip()
            or not isinstance(findings, list)):
        raise malformed
    seen = set()
    active = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise malformed
        id, status = finding.get('finding_id'), finding.get('state')
        if (not _finding_id(id) or id in seen or status not in ('ACTIVE', 'RESOLVED')
                or not all(isinstance(finding.get(key), str) and finding[key].strip() for key in ('path', 'body'))):
            raise malformed
        seen.add(id)
        if status == 'ACTIVE':
            active.append(id)
    return _PersistentRun(run['head_sha'], run['complete'], tuple(active))


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _valid_comment(comment: object) -> bool:
    return (isinstance(comment, Comment) and type(comment.id) is int and comment.id > 0
            and isinstance(comment.author, str) and bool(comment.author.strip())
            and isinstance(comment.body, str) and _aware(comment.created_at) and _aware(comment.updated_at)
            and comment.updated_at >= comment.created_at)


def _newest(comments: list[Comment]) -> Comment:
    # GitHub timestamps can have second precision. Don't guess which conflicting
    # review ran last when the current versions share a timestamp.
    latest = max(comments, key=lambda comment: comment.updated_at)
    if sum(c.updated_at == latest.updated_at for c in comments) != 1:
        raise ReviewBlocked('ambiguous latest PR-Agent review')
    return latest


def _exact_complete(reviewed: str, complete: bool, head: str) -> None:
    if reviewed != head:
        raise ReviewBlocked('PR-Agent reviewed a different head')
    if complete is not True:
        raise ReviewBlocked('PR-Agent review is incomplete')


def _dispositions(body: str) -> set[str]:
    # A dedicated comment consisting solely of disposition lines avoids counting
    # review prose, quotations or Markdown examples as intentional dispositions.
    ids = set()
    for line in body.strip('\r\n').splitlines():
        match = _DISPOSITION.fullmatch(line)
        if match is None or match[1] in ids:
            return set()
        ids.add(match[1])
    return ids


def require_review(evidence: ReviewEvidence | None, pr: IssueRef, head_sha: str,
                   review_author: str, loop_author: str) -> ReviewRun:
    """Require exact-head review and finding coverage, or raise a safe blocker."""
    if (not valid_issue_ref(pr) or not valid_sha(head_sha)
            or not all(isinstance(login, str) and login.strip() for login in (review_author, loop_author))):
        raise ReviewBlocked('expected PR, exact head and trusted review and loop logins required')
    if not isinstance(evidence, ReviewEvidence):
        raise ReviewBlocked('no PR-Agent review evidence')
    if evidence.pr != pr or not isinstance(evidence.comments, tuple):
        raise ReviewBlocked('invalid PR-Agent comment inventory for this PR')
    reviews, persistent = [], []
    seen = set()
    for comment in evidence.comments:
        if not _valid_comment(comment) or comment.id in seen:
            raise ReviewBlocked('invalid PR-Agent comment inventory for this PR')
        seen.add(comment.id)
        if comment.author.casefold() != review_author.casefold():
            continue
        if _STATE_NAMESPACE in comment.body or _FULL_MARKER in comment.body:
            persistent.append(comment)
            reviews.append(comment)
        elif comment.body.split('\n', 1)[0].rstrip('\r') == _STANDALONE_HEADER:
            reviews.append(comment)
    if not reviews:
        raise ReviewBlocked('no PR-Agent review from the configured author')
    latest = _newest(reviews)
    sources = [latest]
    if latest in persistent:
        run = _persistent(latest)
        _exact_complete(run.head_sha, run.complete, head_sha)
        active = set(run.active_ids)
    else:
        standalone = evidence.standalone
        if (not isinstance(standalone, StandaloneRun) or standalone.comment != latest
                or not valid_sha(standalone.head_sha) or not isinstance(standalone.finding_ids, tuple)
                or not all(_finding_id(id) for id in standalone.finding_ids)
                or len(set(standalone.finding_ids)) != len(standalone.finding_ids)):
            raise ReviewBlocked('standalone PR-Agent review lacks trusted head and finding evidence')
        _exact_complete(standalone.head_sha, standalone.complete, head_sha)
        active = set(standalone.finding_ids)
        if persistent:
            canonical = _newest(persistent)
            run = _persistent(canonical)
            if run.head_sha == head_sha:
                _exact_complete(run.head_sha, run.complete, head_sha)
                active.update(run.active_ids)
                sources.append(canonical)

    covered = set()
    for comment in evidence.comments:
        if (comment.author.casefold() == loop_author.casefold()
                and comment.created_at > latest.updated_at):
            covered.update(_dispositions(comment.body))
    missing = sorted(active - covered)
    if missing:
        raise ReviewBlocked('PR-Agent findings lack a later loop disposition: ' + ', '.join(missing))
    # PR-Agent's run_id can be the commit URL, reused across reruns on the same
    # head. Bind E2E to the observed comment version(s) and normalized findings.
    active_ids = tuple(sorted(active))
    versions = [(c.id, c.updated_at.isoformat(), c.body) for c in sources]
    digest = hashlib.sha256(json.dumps((pr.repo, pr.number, head_sha, versions, active_ids)).encode()).hexdigest()
    return ReviewRun(f'pr-agent:{latest.id}:{digest}', active_ids)


def _get_gh(*args: str) -> str:
    try:
        return subprocess.check_output(['gh', 'api', '--method', 'GET', *args], timeout=30,
                                       encoding='utf-8', stderr=subprocess.DEVNULL)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ReviewBlocked('could not read current GitHub PR-Agent evidence') from None


def _gh_head(pr: IssueRef) -> str:
    try:
        data = json.loads(_get_gh(f'repos/{pr.repo}/pulls/{pr.number}'))
    except (ValueError, RecursionError):
        raise ReviewBlocked('could not read PR head from GitHub') from None
    head = data.get('head') if isinstance(data, dict) else None
    if not isinstance(head, dict) or not valid_sha(head.get('sha')):
        raise ReviewBlocked('could not read PR head from GitHub')
    return head['sha']


def read_github_review(pr: IssueRef, head_sha: str) -> ReviewEvidence:
    """Read paginated PR conversation comments; no GitHub mutations or standalone receipt."""
    if (not valid_issue_ref(pr) or not valid_sha(head_sha)
            or not all(_GH_NAME.fullmatch(part) and part not in {'.', '..'} for part in pr.repo.split('/'))):
        raise ReviewBlocked('expected a GitHub owner/repo, PR number and exact head')
    if _gh_head(pr) != head_sha:
        raise ReviewBlocked('PR head does not match the expected head')
    raw = _get_gh(f'repos/{pr.repo}/issues/{pr.number}/comments?per_page=100', '--paginate')
    # gh 2.46 emits concatenated JSON arrays and does not have --slurp.
    raw = raw.strip()
    if not raw:
        raise ReviewBlocked('no GitHub comment pages returned')
    decoder = json.JSONDecoder()
    comments = []
    try:
        start = 0
        while start < len(raw):
            page, start = decoder.raw_decode(raw, start)
            if not isinstance(page, list):
                raise ValueError('not a comment page')
            for row in page:
                comments.append(Comment(row['id'], row['user']['login'], datetime.fromisoformat(row['created_at']),
                                        datetime.fromisoformat(row['updated_at']), row['body']))
            while start < len(raw) and raw[start] in ' \t\r\n':
                start += 1
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ReviewBlocked('invalid GitHub comment pages or metadata') from None
    if _gh_head(pr) != head_sha:
        raise ReviewBlocked('PR head changed while reading review evidence')
    return ReviewEvidence(pr, tuple(comments))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo')
    parser.add_argument('pr', type=int)
    parser.add_argument('--head', required=True)
    parser.add_argument('--review-author', required=True)
    parser.add_argument('--loop-author', required=True)
    args = parser.parse_args(argv)
    pr = IssueRef(args.repo, args.pr)
    try:
        evidence = read_github_review(pr, args.head)
        require_review(evidence, pr, args.head, args.review_author, args.loop_author)
    except ReviewBlocked as exc:
        print(f'PR-Agent gate blocked: {exc}', file=sys.stderr)
        return 2
    print(f'PR-Agent gate passed for {args.repo}#{args.pr} at {args.head}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
