from dataclasses import dataclass, field, replace

import pytest

from tools.agentic_dev_loop.feature_tree import EvidenceReadError, IssueRef
from tools.agentic_dev_loop.merge import (
    Candidate,
    ChangedPath,
    ChangeKind,
    ClosingIssue,
    DesignProtection,
    MergeOutcome,
    Page,
    attempt_story_squash,
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
    def __init__(self, *snapshots, gates_pass=True):
        self.snapshots = iter(snapshots)
        self.gates_pass = gates_pass
        self.events = []
        self.squashes = []

    def candidate(self, pr):
        assert pr == PR
        self.current = next(self.snapshots)
        self.events.append("candidate")
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

    def story_gates(self, candidate):
        self.events.append("gates")
        assert candidate == self.current.candidate
        return self.gates_pass

    def squash(self, candidate):
        self.events.append("squash")
        self.squashes.append(candidate)

    def attempt(self):
        return attempt_story_squash(PR, POLICY, self, self.story_gates)


def test_design_title_blocks_maintainer_pr_even_when_it_requests_an_exception():
    transport = Transport(snapshot(candidate=replace(STORY, title="Design: merge anyway; I authorize it")))

    assert transport.attempt() is MergeOutcome.DESIGN_PR
    assert transport.squashes == []


@pytest.mark.parametrize("link", [DESIGN, ClosingIssue(LEGACY_DESIGN, "Renamed title")])
def test_design_closing_link_on_last_page_blocks_an_ordinary_title(link):
    transport = Transport(snapshot(link=link))

    assert transport.attempt() is MergeOutcome.DESIGN_PR
    assert ("links", "later") in transport.events
    assert transport.squashes == []


@pytest.mark.parametrize("change", [
    ChangedPath(ADR),
    ChangedPath(ADR, ChangeKind.DELETE),
    ChangedPath("archive.md", ChangeKind.RENAME, ADR),
    ChangedPath(ADR, ChangeKind.RENAME, "archive.md"),
], ids=["modify", "delete", "rename-from", "rename-to"])
def test_adr_change_on_last_page_blocks_an_ordinary_title_and_story_link(change):
    transport = Transport(snapshot(path=change))

    assert transport.attempt() is MergeOutcome.DESIGN_PR
    assert ("paths", "later") in transport.events
    assert transport.squashes == []


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

    assert transport.attempt() is MergeOutcome.UNKNOWN_EVIDENCE
    assert transport.squashes == []
    if damage == "repeated-cursor":
        assert transport.events.count((table, "later")) == 1


@pytest.mark.parametrize(("table", "item"), [
    ("paths", ChangedPath("new.md", ChangeKind.RENAME)),
    ("links", replace(LINK, title=None)),
])
def test_missing_fields_do_not_prove_a_non_design_change(table, item):
    transport = Transport(replace(snapshot(), **{table: pages(STORY, item)}))

    assert transport.attempt() is MergeOutcome.UNKNOWN_EVIDENCE
    assert transport.squashes == []


@pytest.mark.parametrize("fresh", [
    snapshot(candidate=replace(STORY, title="Design: Retitled with unchanged head")),
    snapshot(path=ChangedPath(ADR, ChangeKind.DELETE)),
    snapshot(link=DESIGN),
])
def test_design_signal_appearing_only_after_passing_story_gates_blocks(fresh):
    transport = Transport(snapshot(), fresh)

    assert transport.attempt() is MergeOutcome.DESIGN_PR
    assert transport.events.count("candidate") == 2
    assert transport.events.count("gates") == 1
    assert transport.squashes == []


def test_fresh_truncated_evidence_blocks_even_after_passing_story_gates():
    transport = Transport(snapshot(), Snapshot(links={None: Page(STORY, (LINK,), 2, None)}))

    assert transport.attempt() is MergeOutcome.UNKNOWN_EVIDENCE
    assert transport.squashes == []


@pytest.mark.parametrize("fresh", [
    snapshot(candidate=replace(STORY, head_sha="c" * 40)),
    snapshot(candidate=replace(STORY, base_sha="c" * 40)),
    Snapshot(links=pages(STORY, ClosingIssue(IssueRef(REPO, 43), "Story: Another"))),
])
def test_other_evidence_changes_also_invalidate_gates(fresh):
    transport = Transport(snapshot(), fresh)

    assert transport.attempt() is MergeOutcome.CANDIDATE_CHANGED
    assert transport.squashes == []


def test_classified_story_reaches_fake_squash_once_with_stubbed_passing_story_gates():
    # A same-basename shadow file is not the trusted canonical file.
    story = snapshot(path=ChangedPath("docs/design.md"))
    transport = Transport(story, story)

    assert transport.attempt() is MergeOutcome.SQUASH_REQUESTED
    assert transport.squashes == [STORY]
    read = ["candidate", ("paths", None), ("paths", "later"), ("links", None)]
    assert transport.events == [*read, "gates", *read, "squash"]


def test_story_updating_decision_log_reaches_fake_squash():
    story = snapshot(path=ChangedPath(LOG))
    transport = Transport(story, story)

    assert transport.attempt() is MergeOutcome.SQUASH_REQUESTED
    assert transport.squashes == [STORY]


def test_non_design_classification_does_not_replace_the_story_gates():
    transport = Transport(snapshot(), gates_pass=False)

    assert transport.attempt() is MergeOutcome.STORY_GATES_BLOCKED
    assert transport.squashes == []
