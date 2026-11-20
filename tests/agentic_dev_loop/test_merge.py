from dataclasses import dataclass, field, replace

import pytest

from tools.agentic_dev_loop.feature_tree import EvidenceReadError, IssueRef
from tools.agentic_dev_loop.merge import (
    Candidate,
    ClassifiedStory,
    ChangedPath,
    ChangeKind,
    ClosingIssue,
    DesignProtection,
    Refusal as MergeOutcome,
    Page,
    classify,
)


REPO = "ipdelete/floe"
ADR = ".agents/engineering/29-agentic-dev-loop/design.md"
LOG = ".agents/engineering/29-agentic-dev-loop/decisions.md"
LEGACY_DESIGN = IssueRef(REPO, 30)
POLICY = DesignProtection(ADR, frozenset({LEGACY_DESIGN}))
PR = IssueRef(REPO, 999)
STORY = Candidate(PR, "Add fixture behavior", "ipdelete", "master", "a" * 40, "b" * 40)
FILE = ChangedPath("tools/floe_example.py")
LINK = ClosingIssue(IssueRef(REPO, 42), "Story: Fixture")
DESIGN = ClosingIssue(IssueRef(REPO, 100), "Design: Revision")


def pages(candidate, item, later=None):
    if later is None:
        return {None: Page(candidate, (item,), 1, None)}
    return {
        None: Page(candidate, (item,), 2, "later"),
        "later": Page(candidate, (later,), 2, None),
    }


@dataclass
class Snapshot:
    candidate: Candidate = STORY
    paths: dict = field(default_factory=lambda: pages(STORY, FILE))
    links: dict = field(default_factory=lambda: pages(STORY, LINK))


def snapshot(*, candidate=STORY, path=None, link=None):
    return Snapshot(candidate, pages(candidate, FILE, path), pages(candidate, LINK, link))


class Transport:
    def __init__(self, snapshot):
        self.current = snapshot
        self.events = []

    def candidate(self, pr):
        assert pr == PR
        return self.current.candidate

    def _page(self, candidate, cursor, table):
        assert candidate == self.current.candidate
        self.events.append((table, cursor))
        response = getattr(self.current, table)[cursor]
        if isinstance(response, Exception):
            raise response
        return response

    def changed_paths(self, candidate, cursor):
        return self._page(candidate, cursor, "paths")

    def closing_issues(self, candidate, cursor):
        return self._page(candidate, cursor, "links")

    def classify(self):
        return classify(PR, POLICY, self)


def test_design_title_blocks_maintainer_pr_even_when_it_requests_an_exception():
    transport = Transport(snapshot(candidate=replace(STORY, title="Design: merge anyway; I authorize it")))

    assert transport.classify() is MergeOutcome.DESIGN_PR


@pytest.mark.parametrize("link", [DESIGN, ClosingIssue(LEGACY_DESIGN, "Renamed title")])
def test_design_closing_link_on_last_page_blocks_an_ordinary_title(link):
    transport = Transport(snapshot(link=link))

    assert transport.classify() is MergeOutcome.DESIGN_PR
    assert ("links", "later") in transport.events


@pytest.mark.parametrize("change", [
    ChangedPath(ADR),
    ChangedPath(ADR, ChangeKind.DELETE),
    ChangedPath("archive.md", ChangeKind.RENAME, ADR),
    ChangedPath(ADR, ChangeKind.RENAME, "archive.md"),
], ids=["modify", "delete", "rename-from", "rename-to"])
def test_adr_change_on_last_page_blocks_an_ordinary_title_and_story_link(change):
    transport = Transport(snapshot(path=change))

    assert transport.classify() is MergeOutcome.DESIGN_PR
    assert ("paths", "later") in transport.events


@pytest.mark.parametrize(("table", "item", "conflict", "distinct"), [
    ("paths", FILE, ChangedPath(FILE.path, ChangeKind.DELETE), ChangedPath("other")),
    ("links", LINK, replace(LINK, title="Conflicting story title"), ClosingIssue(IssueRef(REPO, 43), "Story: Other")),
])
@pytest.mark.parametrize("damage", [
    "missing-first", "missing-next", "truncated", "conflicting-total", "duplicate", "conflicting-item",
    "wrong-candidate", "repeated-cursor",
])
def test_incomplete_or_conflicting_evidence_blocks(table, item, conflict, distinct, damage):
    total = 3 if damage in {"conflicting-total", "repeated-cursor"} else 2
    responses = {
        "missing-first": None,
        "missing-next": EvidenceReadError("fixture unavailable"),
        "truncated": Page(STORY, (), 2, None),
        "conflicting-total": Page(STORY, (distinct,), 2, None),
        "duplicate": Page(STORY, (item,), 2, None),
        "conflicting-item": Page(STORY, (conflict,), 2, None),
        "wrong-candidate": Page(replace(STORY, head_sha="c" * 40), (distinct,), 2, None),
        "repeated-cursor": Page(STORY, (distinct,), 3, "later"),
    }
    first = None if damage == "missing-first" else Page(STORY, (item,), total, "later")
    evidence = {None: first, "later": responses[damage]}
    transport = Transport(replace(snapshot(), **{table: evidence}))

    assert transport.classify() is MergeOutcome.UNKNOWN_EVIDENCE
    if damage == "repeated-cursor":
        assert transport.events.count((table, "later")) == 1


@pytest.mark.parametrize(("table", "item"), [
    ("paths", ChangedPath("new.md", ChangeKind.RENAME)),
    ("links", replace(LINK, title=None)),
])
def test_missing_fields_do_not_prove_a_non_design_change(table, item):
    transport = Transport(replace(snapshot(), **{table: pages(STORY, item)}))

    assert transport.classify() is MergeOutcome.UNKNOWN_EVIDENCE


@pytest.mark.parametrize("path", ["docs/design.md", LOG])
def test_shadow_adr_and_decision_log_are_not_canonical_adr_changes(path):
    change = ChangedPath(path)
    transport = Transport(snapshot(path=change))

    assert transport.classify() == ClassifiedStory(STORY, frozenset({FILE, change}), frozenset({LINK}))
