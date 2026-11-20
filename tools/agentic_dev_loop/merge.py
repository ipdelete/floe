"""Trusted design-PR classification over normalized fixture evidence."""

import re
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .feature_tree import EvidenceReadError, IssueRef, valid_issue_ref


@dataclass(frozen=True)
class Candidate:
    pr: IssueRef
    title: str
    author: str
    base: str
    base_sha: str
    head_sha: str


class ChangeKind(StrEnum):
    CHANGE = "change"  # any non-rename, non-deletion change
    DELETE = "delete"
    RENAME = "rename"


@dataclass(frozen=True)
class ChangedPath:
    path: str  # original path for a deletion, destination for a rename
    kind: ChangeKind = ChangeKind.CHANGE
    previous_path: str | None = None  # required for a rename


@dataclass(frozen=True)
class ClosingIssue:
    issue: IssueRef
    title: str


@dataclass(frozen=True)
class DesignProtection:
    adr_path: str
    design_issues: frozenset[IssueRef]  # known identities regardless of current title


@dataclass(frozen=True)
class Page[T]:
    candidate: Candidate
    items: tuple[T, ...]
    total: int  # authoritative count of all records, not the number the endpoint can return
    next_cursor: str | None


class CandidateReader(Protocol):
    """Fresh normalized facts; raise EvidenceReadError if completeness cannot be verified.

    Pages must be bound to the supplied candidate, not stamped with an assumed
    identity. `None` cursor after a page means verified exhaustion, not a cap or
    a missing cursor. Closing links are actual GitHub edges, not PR body text.
    Translate expected I/O and evidence failures at the adapter boundary;
    unexpected programming errors are not treated as missing evidence.
    """

    def candidate(self, pr: IssueRef) -> Candidate: ...

    def changed_paths(self, candidate: Candidate, cursor: str | None) -> Page[ChangedPath]: ...

    def closing_issues(self, candidate: Candidate, cursor: str | None) -> Page[ClosingIssue]: ...


class Refusal(StrEnum):
    DESIGN_PR = "design-pr"
    UNKNOWN_EVIDENCE = "unknown-evidence"


@dataclass(frozen=True)
class ClassifiedStory:
    candidate: Candidate
    paths: frozenset[ChangedPath]
    links: frozenset[ClosingIssue]


def _issue_key(issue: IssueRef) -> tuple[str, int]:
    return issue.repo.casefold(), issue.number


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _repo_path(value: object) -> bool:
    return (isinstance(value, str) and "\0" not in value
            and all(part not in {"", ".", ".."} for part in value.split("/")))


def _path_keys(item: ChangedPath) -> tuple[str, ...]:
    if (not isinstance(item, ChangedPath) or not _repo_path(item.path)
            or not isinstance(item.kind, ChangeKind)):
        raise EvidenceReadError("malformed changed path")
    if item.kind is ChangeKind.RENAME:
        if not _repo_path(item.previous_path) or item.previous_path == item.path:
            raise EvidenceReadError("missing or invalid rename source")
        return item.path, item.previous_path
    if item.previous_path is not None:
        raise EvidenceReadError("unexpected rename source")
    return (item.path,)


def _link_keys(item: ClosingIssue) -> tuple[tuple[str, int]]:
    if (not isinstance(item, ClosingIssue) or not valid_issue_ref(item.issue)
            or not _nonempty_text(item.title)):
        raise EvidenceReadError("malformed closing link")
    return (_issue_key(item.issue),)


def _read_pages[T](
    candidate: Candidate,
    read: Callable[[Candidate, str | None], Page[T]],
    keys: Callable[[T], tuple[Hashable, ...]],
) -> frozenset[T]:
    cursor = None
    seen_cursors: set[str] = set()
    seen_keys: set[Hashable] = set()
    items: list[T] = []
    total = None
    while True:
        page = read(candidate, cursor)
        if (not isinstance(page, Page) or page.candidate != candidate
                or not isinstance(page.items, tuple)
                or type(page.total) is not int or page.total < 0
                or (total is not None and total != page.total)):
            raise EvidenceReadError("missing or inconsistent evidence page")
        total = page.total
        for item in page.items:
            identities = keys(item)
            if not seen_keys.isdisjoint(identities):
                raise EvidenceReadError("duplicate or conflicting evidence")
            seen_keys.update(identities)
            items.append(item)

        cursor = page.next_cursor
        if cursor is None:
            if len(items) != total:
                raise EvidenceReadError("evidence count does not match exhausted connection")
            return frozenset(items)
        if (not _nonempty_text(cursor) or cursor in seen_cursors or not page.items
                or len(items) >= total):
            raise EvidenceReadError("evidence continuation does not make progress")
        seen_cursors.add(cursor)


def classify(
    pr: IssueRef, policy: DesignProtection, transport: CandidateReader,
) -> ClassifiedStory | Refusal:
    if not valid_issue_ref(pr):
        raise ValueError("a repository-qualified PR number is required")
    if (not isinstance(policy, DesignProtection) or not _repo_path(policy.adr_path)
            or not isinstance(policy.design_issues, frozenset)
            or not all(valid_issue_ref(issue) for issue in policy.design_issues)):
        raise ValueError("the canonical ADR path and known design identities are required")
    try:
        candidate = transport.candidate(pr)
        if (not isinstance(candidate, Candidate) or candidate.pr != pr
                or not valid_issue_ref(candidate.pr) or not _nonempty_text(candidate.title)):
            return Refusal.UNKNOWN_EVIDENCE
        if candidate.title.startswith("Design:"):
            return Refusal.DESIGN_PR
        if (not _nonempty_text(candidate.author) or not _nonempty_text(candidate.base)
                or not _sha(candidate.base_sha) or not _sha(candidate.head_sha)):
            return Refusal.UNKNOWN_EVIDENCE

        paths = _read_pages(candidate, transport.changed_paths, _path_keys)
        if any(policy.adr_path in (path.path, path.previous_path) for path in paths):
            return Refusal.DESIGN_PR

        links = _read_pages(candidate, transport.closing_issues, _link_keys)
        known_designs = {_issue_key(issue) for issue in policy.design_issues}
        if any(link.title.startswith("Design:") or _issue_key(link.issue) in known_designs for link in links):
            return Refusal.DESIGN_PR
    except (EvidenceReadError, OSError):
        return Refusal.UNKNOWN_EVIDENCE
    return ClassifiedStory(candidate, paths, links)
