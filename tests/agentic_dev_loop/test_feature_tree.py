from dataclasses import dataclass, field

import pytest

from tools.agentic_dev_loop.feature_tree import (
    ChildIssue,
    EvidenceReadError,
    FeatureTree,
    IssueRef,
    Phase,
    SubIssuePage,
    UnknownTree,
    read_feature_trees,
)


FEATURE = IssueRef("ipdelete/floe", 29)
OTHER = IssueRef("ipdelete/floe", 100)


def child(number, title, *, parent=FEATURE, repo=FEATURE.repo, body=""):
    return ChildIssue(IssueRef(repo, number), parent, title, body)


@dataclass
class FixtureTransport:
    pages: dict[tuple[IssueRef, str | None], SubIssuePage | EvidenceReadError]
    requests: list[tuple[IssueRef, str | None]] = field(default_factory=list)

    def sub_issues(self, feature, cursor):
        self.requests.append((feature, cursor))
        result = self.pages[feature, cursor]
        if isinstance(result, EvidenceReadError):
            raise result
        return result


def test_follows_actual_edges_in_page_order_and_classifies_by_issue_number():
    legacy_numbers = (35, 30, 32, 31, 33, 34)
    legacy = tuple(
        child(number, "Story: Misleading rename" if number == 30 else "Renamed")
        for number in legacy_numbers
    )
    transport = FixtureTransport({
        (FEATURE, None): SubIssuePage((
            child(39, "Story: First page", body="Related work:\n- [ ] #38"),
            child(40, "Story: Different parent", parent=OTHER),
            child(41, "Spike: Same parent number, different repo", parent=IssueRef("elsewhere/floe", 29)),
            child(42, "Story: No parent", parent=None),
            child(43, "Story: Different repo", repo="elsewhere/floe"),
            child(44, "Not a phase"),
        ), "later"),
        (FEATURE, "later"): SubIssuePage((
            *legacy,
            child(37, "Story: Later page"),
            child(45, "Spike: New spike"),
            child(46, "Design: New design"),
        ), None),
    })

    tree = read_feature_trees(transport, [FEATURE])[FEATURE]

    assert isinstance(tree, FeatureTree)
    assert list(tree.phases) == [39, *legacy_numbers, 37, 45, 46]
    assert tree.phases == {
        39: Phase.STORY,
        30: Phase.DESIGN,
        **dict.fromkeys(range(31, 36), Phase.SPIKE),
        37: Phase.STORY,
        45: Phase.SPIKE,
        46: Phase.DESIGN,
    }
    assert transport.requests == [(FEATURE, None), (FEATURE, "later")]


def test_legacy_roles_do_not_override_new_discovery_in_a_different_feature():
    transport = FixtureTransport({
        (OTHER, None): SubIssuePage((child(30, "Story: Not a legacy design here", parent=OTHER),), None),
    })

    assert read_feature_trees(transport, [OTHER])[OTHER] == FeatureTree({30: Phase.STORY})


def test_legacy_mapping_does_not_supply_missing_edges():
    transport = FixtureTransport({
        (FEATURE, None): SubIssuePage((child(30, "Design: Foreign parent", parent=OTHER),), None),
    })

    assert read_feature_trees(transport, [FEATURE])[FEATURE] == FeatureTree({})


@pytest.mark.parametrize(("second_page", "reason"), [
    (EvidenceReadError("fixture outage"), "page-read-failed"),
    (SubIssuePage((), "later"), "repeated-cursor"),
    (SubIssuePage((), ""), "invalid-cursor"),
    (SubIssuePage((child(29, "Story: Self-edge"),), None), "invalid-child"),
    (SubIssuePage((child(37, "Design: Conflicting title"),), None), "duplicate-child"),
    (SubIssuePage((child(37, "Story: First", parent=OTHER),), None), "duplicate-child"),
    (SubIssuePage((child(37, "Story: First"),), None), "duplicate-child"),
])
def test_unknown_feature_does_not_hide_an_independent_feature(second_page, reason):
    transport = FixtureTransport({
        (FEATURE, None): SubIssuePage((child(37, "Story: First"),), "later"),
        (FEATURE, "later"): second_page,
        (OTHER, None): SubIssuePage((child(101, "Story: Independent", parent=OTHER),), None),
    })

    trees = read_feature_trees(transport, iter([FEATURE, OTHER]))

    assert trees == {FEATURE: UnknownTree(reason), OTHER: FeatureTree({101: Phase.STORY})}
    assert transport.requests == [(FEATURE, None), (FEATURE, "later"), (OTHER, None)]


def test_failed_first_page_is_not_known_empty():
    transport = FixtureTransport({
        (FEATURE, None): EvidenceReadError("fixture outage"),
        (OTHER, None): SubIssuePage((), None),
    })

    assert read_feature_trees(transport, [FEATURE, OTHER]) == {
        FEATURE: UnknownTree("page-read-failed"),
        OTHER: FeatureTree({}),
    }
