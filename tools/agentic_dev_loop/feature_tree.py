"""Inventory feature phases from normalized GitHub sub-issue evidence."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol


@dataclass(frozen=True)
class IssueRef:
    """Issue number qualified by a canonical ``owner/repo`` name."""

    repo: str
    number: int


@dataclass(frozen=True)
class ChildIssue:
    issue: IssueRef
    parent: IssueRef | None
    title: str
    body: str = ""


@dataclass(frozen=True)
class SubIssuePage:
    children: tuple[ChildIssue, ...]
    next_cursor: str | None


class EvidenceReadError(Exception):
    """The transport could not supply a complete page of evidence."""


class SubIssueTransport(Protocol):
    def sub_issues(self, feature: IssueRef, cursor: str | None) -> SubIssuePage:
        """Read an ordered page of actual edges, starting at ``cursor=None``.

        ``next_cursor=None`` means the connection is exhausted. An edge's
        ``parent=None`` means a confirmed missing parent. Raise EvidenceReadError
        on any incomplete read, including missing cursor or parent evidence.
        """
        ...


class Phase(StrEnum):
    SPIKE = "spike"
    DESIGN = "design"
    STORY = "story"


_LEGACY_FEATURE = IssueRef("ipdelete/floe", 29)
_LEGACY_PHASES = {30: Phase.DESIGN, **dict.fromkeys(range(31, 36), Phase.SPIKE)}
_TITLE_PHASES = (("Spike:", Phase.SPIKE), ("Design:", Phase.DESIGN), ("Story:", Phase.STORY))


@dataclass(frozen=True)
class FeatureTree:
    """Complete phase inventory keyed by issue number in sub-issue order."""

    phases: Mapping[int, Phase]

    def __post_init__(self):
        object.__setattr__(self, "phases", MappingProxyType(dict(self.phases)))


@dataclass(frozen=True)
class UnknownTree:
    """The feature is blocked on evidence; no partial inventory is usable."""

    reason: str


def _valid_ref(issue: object) -> bool:
    if not isinstance(issue, IssueRef) or not isinstance(issue.repo, str):
        return False
    owner, separator, repo = issue.repo.partition("/")
    return bool(owner and separator and repo and "/" not in repo
                and type(issue.number) is int and issue.number > 0)


def _valid_child(child: object) -> bool:
    return (isinstance(child, ChildIssue) and _valid_ref(child.issue)
            and (child.parent is None or _valid_ref(child.parent))
            and isinstance(child.title, str))


def _phase(feature: IssueRef, child: ChildIssue) -> Phase | None:
    if feature == _LEGACY_FEATURE:
        legacy = _LEGACY_PHASES.get(child.issue.number)
        if legacy is not None:
            return legacy
    for prefix, phase in _TITLE_PHASES:
        if child.title.startswith(prefix):
            return phase
    return None


def _read_feature_tree(transport: SubIssueTransport, feature: IssueRef) -> FeatureTree | UnknownTree:
    phases: dict[int, Phase] = {}
    seen_children: set[IssueRef] = set()
    seen_cursors: set[str] = set()
    cursor = None
    while True:
        try:
            page = transport.sub_issues(feature, cursor)
        except EvidenceReadError:
            return UnknownTree("page-read-failed")
        if not isinstance(page, SubIssuePage) or not isinstance(page.children, tuple):
            return UnknownTree("invalid-page")
        for child in page.children:
            if not _valid_child(child) or child.issue == feature:
                return UnknownTree("invalid-child")
            if child.issue in seen_children:
                # Even identical repeats can signal a shifting page boundary.
                return UnknownTree("duplicate-child")
            seen_children.add(child.issue)
            if child.parent != feature or child.issue.repo != feature.repo:
                continue
            phase = _phase(feature, child)
            if phase is not None:
                phases[child.issue.number] = phase

        cursor = page.next_cursor
        if cursor is None:
            return FeatureTree(phases)
        if not isinstance(cursor, str) or not cursor:
            return UnknownTree("invalid-cursor")
        if cursor in seen_cursors:
            return UnknownTree("repeated-cursor")
        seen_cursors.add(cursor)


def read_feature_trees(
    transport: SubIssueTransport, features: Iterable[IssueRef],
) -> dict[IssueRef, FeatureTree | UnknownTree]:
    """Read known feature indexes independently, once each, in input order.

    Inventory only direct children in the feature's own repository. Titles
    classify phases, not membership; issue bodies never supply edges.
    """
    requested = tuple(features)
    if not all(_valid_ref(feature) for feature in requested):
        raise ValueError("features must be repository-qualified issue numbers")
    return {feature: _read_feature_tree(transport, feature) for feature in dict.fromkeys(requested)}
