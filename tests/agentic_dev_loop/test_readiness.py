from dataclasses import replace

import pytest

from tools.agentic_dev_loop.feature_tree import FeatureTree, IssueRef, Phase, UnknownReason, UnknownTree
from tools.agentic_dev_loop.readiness import (
    AdrAtMerge,
    ApprovedDesign,
    Backlog,
    Blocker,
    DesignEvidence,
    FeatureEvidence,
    IssueState,
    Policy,
    PullRequest,
    PullRequestState,
    Reason,
    Role,
    SpikeEvidence,
    SpikeHandoff,
    SpikeResult,
    StoryCompletion,
    StoryEvidence,
    Work,
    decide_feature,
)


FEATURE = IssueRef("example/floe", 101)
OTHER = IssueRef("example/floe", 102)
ADR = ".agents/engineering/101-feature/design.md"
POLICY = Policy(maintainer="keeper", adr_path=ADR)
FIRST, SECOND, SQUASH, PIN = (c * 40 for c in "abcd")
OPEN, CLOSED = IssueState.OPEN, IssueState.CLOSED


def pr(number, child, sha=None, **changes):
    value = PullRequest(
        number=number, repo=FEATURE.repo, base="master", closes=(IssueRef(FEATURE.repo, child),),
        state=PullRequestState.MERGED if sha else PullRequestState.OPEN,
        squash_sha=sha, squash_verified=sha is not None, merged_by="keeper" if sha else None,
    )
    return replace(value, **changes)


def design(number=1, sha=FIRST, previous=None):
    candidate = pr(20 + number, number, sha)
    return DesignEvidence(
        CLOSED, False, previous, (candidate,),
        AdrAtMerge(ADR, sha, "100644", changed_in_pr=True),
    )


def feature(*, designs=None, spikes=None, stories=None, **changes):
    designs = {1: design()} if designs is None else designs
    spikes = {} if spikes is None else spikes
    stories = {} if stories is None else stories
    tree = FeatureTree({
        **dict.fromkeys(spikes, Phase.SPIKE),
        **dict.fromkeys(designs, Phase.DESIGN),
        **dict.fromkeys(stories, Phase.STORY),
    })
    if "backlog" in changes:
        changes["backlogs"] = (*changes.pop("backlogs", ()), changes.pop("backlog"))
    return replace(FeatureEvidence(tree, {**spikes, **designs, **stories}, OPEN, False, False), **changes)


def story(*, sha=FIRST, **changes):
    if "candidates" in changes:
        changes["candidates"] = tuple(
            replace(candidate, approved_revision=candidate.approved_revision or sha)
            for candidate in changes["candidates"]
        )
    return replace(StoryEvidence(OPEN, False, sha, (), None, ()), **changes)


def completed(number, *, revision=FIRST):
    candidate = pr(30 + number, number, SQUASH)
    return story(sha=revision, state=CLOSED, expected_pr=candidate.number, candidates=(candidate,))


def decide(snapshot):
    return decide_feature(FEATURE, POLICY, snapshot)


def test_spike_needs_all_evidence_bound_to_the_actual_design_and_same_commit():
    open_design = DesignEvidence(OPEN, False, None, (), None)
    full = SpikeEvidence(
        CLOSED, False, 1,
        result=SpikeResult("refs/heads/spike/101-2", PIN, retained=True),
        handoff=SpikeHandoff(1, PIN),
    )
    for incomplete in (
        SpikeEvidence(CLOSED, False, 1),
        replace(full, handoff=None),
        replace(full, handoff=SpikeHandoff(99, PIN)),
        replace(full, handoff=SpikeHandoff(1, SECOND)),
        replace(full, result=replace(full.result, retained=False)),
        replace(full, result=replace(full.result, branch="refs/tags/spike")),
    ):
        result = decide(feature(designs={1: open_design}, spikes={2: incomplete}))
        assert result.work is None
        assert Blocker(2, Reason.SPIKE_HANDOFF) in result.blockers

    for sufficient in (full, SpikeEvidence(OPEN, False, 1, disposed_for=1)):
        result = decide(feature(designs={1: open_design}, spikes={2: sufficient}))
        assert result.work == Work(Role.DESIGNER, 1)

    result = decide(feature(designs={1: open_design}, spikes={2: replace(full, state=OPEN)}))
    assert result.work == Work(Role.SPIKE, 2)


def test_design_approval_pins_the_canonical_adr_to_the_maintainer_squash():
    result = decide(feature())
    assert result.approval == ApprovedDesign(1, 21, FIRST, ADR)
    assert result.work == Work(Role.PLANNER, FEATURE.number, result.approval)
    assert result.blockers == ()


@pytest.mark.parametrize("change", [
    {"state": PullRequestState.OPEN, "squash_sha": None, "squash_verified": False, "merged_by": None},
    {"merged_by": "someone-else"},
    {"repo": "fork/floe"},
    {"base": "dev"},
    {"closes": (IssueRef(FEATURE.repo, 2),)},
    {"closes": (IssueRef(FEATURE.repo, True),)},  # bool equals 1 but isn't an issue number
    {"closes": (IssueRef("elsewhere/floe", 1),)},
    {"closes": (IssueRef(FEATURE.repo, 1), IssueRef(FEATURE.repo, 99))},
    {"squash_verified": False},
])
def test_nonmatching_design_pr_does_not_unlock_planning(change):
    evidence = design()
    result = decide(feature(designs={1: replace(evidence, candidates=(replace(evidence.candidates[0], **change),))}))
    assert result.approval is None
    assert result.work is None
    assert result.blockers


@pytest.mark.parametrize("adr", [
    None,  # deleted or renamed away: no canonical entry at the squash
    AdrAtMerge(f"shadow/{ADR}", FIRST, "100644", True),
    AdrAtMerge(ADR, SECOND, "100644", True),
    AdrAtMerge(ADR, FIRST, "120000", True),
    AdrAtMerge(ADR, FIRST, "040000", True),
    AdrAtMerge(ADR, FIRST, "100644", False),  # pre-existing ADR, PR only touched a shadow path
])
def test_untrusted_or_unchanged_adr_does_not_unlock_planning(adr):
    result = decide(feature(designs={1: replace(design(), adr=adr)}))
    assert result.approval is None
    assert result.work is None
    assert Blocker(1, Reason.INVALID_ADR) in result.blockers


def test_subissue_order_is_priority_not_dependency_and_ineligible_stories_are_explained():
    stories = {5: story(prerequisites=(8,)), 6: story(active=True), 7: story(), 8: story(), 9: story()}
    result = decide(feature(stories=stories, backlog=Backlog(FIRST, (9, 8, 6, 5), (7,))))
    assert result.work == Work(Role.BUILDER, 8, result.approval)
    assert result.blockers == (
        Blocker(5, Reason.UNMET_PREREQUISITE, (8,)),
        Blocker(6, Reason.ACTIVE_WORK),
        Blocker(7, Reason.SUPERSEDED),
    )
    assert not result.merge_candidates


def test_unknown_or_ambiguous_evidence_stops_only_its_feature():
    approved = design()
    duplicate = replace(approved, candidates=approved.candidates * 2)
    for blocked in (
        feature(designs={1: duplicate}),
        feature(designs={1: replace(approved, candidates=None)}),
        feature(tree=UnknownTree(UnknownReason.PAGE_READ_FAILED)),
        feature(designs={1: approved, 2: design(2, SECOND, 1), 3: design(3, SECOND, 1)}),
        feature(stories={5: story(active=None), 6: story()}, backlog=Backlog(FIRST, (5, 6))),
        feature(stories={5: story(candidates=(pr(35, 5),) * 2), 6: story()}, backlog=Backlog(FIRST, (5, 6))),
    ):
        decisions = {
            ref: decide_feature(ref, POLICY, snapshot)
            for ref, snapshot in ((FEATURE, blocked), (OTHER, feature()))
        }
        assert decisions[FEATURE].work is None
        assert decisions[FEATURE].blockers
        assert not decisions[FEATURE].merge_candidates
        assert decisions[OTHER].work == Work(Role.PLANNER, OTHER.number, decisions[OTHER].approval)


def test_verified_completion_unlocks_prerequisite_then_closeout_and_records_squash():
    backlog = Backlog(FIRST, (5, 6))
    result = decide(feature(stories={5: completed(5), 6: story(prerequisites=(5,))}, backlog=backlog))
    assert result.completed_stories == (StoryCompletion(5, 35, SQUASH),)
    assert result.work == Work(Role.BUILDER, 6, result.approval)

    result = decide(feature(stories={5: completed(5), 6: completed(6)}, backlog=backlog))
    assert result.completed_stories == (StoryCompletion(5, 35, SQUASH), StoryCompletion(6, 36, SQUASH))
    assert result.work == Work(Role.ARCHIVIST, FEATURE.number, result.approval)


@pytest.mark.parametrize("invalid, reason", [
    (story(state=CLOSED), Reason.STORY_DISCREPANCY),
    (story(expected_pr=35, candidates=(pr(35, 5, SQUASH, state=PullRequestState.OPEN),)),
     Reason.STORY_DISCREPANCY),
    (replace(completed(5), state=OPEN), Reason.STORY_DISCREPANCY),
    (replace(completed(5), expected_pr=99), Reason.STORY_DISCREPANCY),
    (replace(completed(5), expected_pr=None), Reason.STORY_DISCREPANCY),
    (replace(completed(5), candidates=(pr(35, 5, SQUASH, squash_verified=False, approved_revision=FIRST),)), Reason.STORY_DISCREPANCY),
    (replace(completed(5), candidates=(pr(35, 5, "not-a-sha", approved_revision=FIRST),)), Reason.STORY_DISCREPANCY),
    (replace(completed(5), candidates=(pr(35, 7, SQUASH),)), Reason.STORY_DISCREPANCY),
    (replace(completed(5), candidates=(pr(35, 5, SQUASH, base="dev"),)), Reason.STORY_DISCREPANCY),
])
def test_story_completion_discrepancy_blocks_even_if_another_story_is_ready(invalid, reason):
    result = decide(feature(stories={5: invalid, 6: story()}, backlog=Backlog(FIRST, (5, 6))))
    assert result.completed_stories == ()
    assert result.work is None
    assert Blocker(5, reason) in result.blockers


def test_pending_revision_or_judgment_blocks_builder_merge_scope_and_archivist():
    pending = DesignEvidence(OPEN, False, 1, (), None)
    current_candidate = story(expected_pr=35, candidates=(pr(35, 5),))
    snapshot = feature(stories={5: current_candidate, 6: story()}, backlog=Backlog(FIRST, (5, 6)))
    result = decide(snapshot)
    assert result.work == Work(Role.BUILDER, 6, result.approval)
    assert result.merge_candidates == (35,)  # scope only, not merge authorization

    result = decide(replace(snapshot, pending_judgment=True))
    assert result.work is None and not result.merge_candidates
    assert Blocker(FEATURE.number, Reason.PENDING_JUDGMENT) in result.blockers

    result = decide(feature(designs={1: design(), 2: pending}, stories={5: current_candidate, 6: story()}, backlogs=snapshot.backlogs))
    assert result.work == Work(Role.DESIGNER, 2, result.approval)
    assert result.approval.sha == FIRST
    assert not result.merge_candidates
    assert Blocker(2, Reason.PENDING_DESIGN) in result.blockers

    done = feature(stories={5: completed(5)}, backlog=Backlog(FIRST, (5,)))
    assert decide(replace(done, pending_judgment=True)).work is None
    blocked = feature(designs={1: design(), 2: replace(pending, candidates=(pr(22, 2),))},
                      stories={5: completed(5)}, backlogs=done.backlogs)
    assert decide(blocked).work is None


def test_revised_backlog_cannot_silently_drop_stories_or_reuse_stale_evidence():
    designs = {2: design(2, SECOND, 1), 1: design()}  # priority is not revision chronology
    stories = {5: completed(5), 6: story()}
    result = decide(feature(designs=designs, stories=stories, backlog=Backlog(FIRST, (5, 6))))
    assert result.approval == ApprovedDesign(2, 22, SECOND, ADR)
    assert result.work == Work(Role.PLANNER, FEATURE.number, result.approval)
    assert not result.completed_stories and not result.merge_candidates

    result = decide(feature(designs=designs, stories=stories, backlog=Backlog(SECOND, (6,))))
    assert result.work is None
    assert Blocker(FEATURE.number, Reason.INVALID_BACKLOG) in result.blockers

    backlog = Backlog(SECOND, (6,), (5,))
    result = decide(feature(designs=designs, stories=stories, backlog=backlog))
    assert result.work is None
    assert Blocker(6, Reason.STALE_STORY) in result.blockers

    result = decide(feature(designs=designs, stories={5: completed(5), 6: story(sha=SECOND)}, backlog=backlog))
    assert result.work == Work(Role.BUILDER, 6, result.approval)
    assert result.completed_stories == ()
    assert Blocker(5, Reason.SUPERSEDED) in result.blockers

    result = decide(feature(designs=designs, stories={5: completed(5), 6: completed(6, revision=SECOND)}, backlog=backlog))
    assert result.work == Work(Role.ARCHIVIST, FEATURE.number, result.approval)
    assert result.completed_stories == (StoryCompletion(6, 36, SQUASH),)


def test_unlinking_a_previously_planned_story_does_not_supersede_it():
    designs = {1: design(), 2: design(2, SECOND, 1)}
    previous = Backlog(FIRST, (5, 6))
    stories = {6: completed(6, revision=SECOND)}
    dropped = decide(feature(designs=designs, stories=stories, backlogs=(previous, Backlog(SECOND, (6,)))))
    assert dropped.work is None
    assert Blocker(FEATURE.number, Reason.INVALID_BACKLOG) in dropped.blockers

    reconciled = decide(feature(designs=designs, stories=stories, backlogs=(previous, Backlog(SECOND, (6,), (5,)))))
    assert reconciled.work == Work(Role.ARCHIVIST, FEATURE.number, reconciled.approval)
    assert Blocker(5, Reason.SUPERSEDED) in reconciled.blockers


def test_invalid_intermediate_plan_cannot_erase_an_older_story():
    designs = {1: design(), 2: design(2, SECOND, 1), 3: design(3, PIN, 2)}
    history = (Backlog(PIN, (6,)), Backlog(FIRST, (5, 6)), Backlog(SECOND, (6,)))
    result = decide(feature(designs=designs, stories={6: completed(6, revision=PIN)}, backlogs=history))
    assert result.work is None
    assert Blocker(FEATURE.number, Reason.INVALID_BACKLOG) in result.blockers


def test_spike_handoff_to_a_different_linked_design_is_not_enough():
    spike = SpikeEvidence(CLOSED, False, 1, SpikeResult("refs/heads/spike/101-3", PIN, True), SpikeHandoff(2, PIN))
    result = decide(feature(designs={1: design(), 2: DesignEvidence(OPEN, False, 1, (), None)}, spikes={3: spike}))
    assert result.work is None
    assert Blocker(3, Reason.SPIKE_HANDOFF) in result.blockers


def test_ambiguous_design_evidence_blocks_spike_offers_too():
    proposed = pr(21, 1)
    result = decide(feature(
        designs={1: DesignEvidence(OPEN, False, None, (proposed, proposed), None)},
        spikes={2: SpikeEvidence(OPEN, False, 1)},
    ))
    assert result.work is None
    assert Blocker(1, Reason.AMBIGUOUS_PR) in result.blockers


def test_unknown_backlog_history_and_active_planner_do_not_offer_another_worker():
    unknown = decide(feature(backlogs=None))
    assert unknown.work is None
    assert Blocker(FEATURE.number, Reason.UNKNOWN_EVIDENCE) in unknown.blockers
    active = decide(feature(planner_active=True))
    assert active.work is None
    assert Blocker(FEATURE.number, Reason.ACTIVE_WORK) in active.blockers


def test_closeout_requires_a_published_plan_but_confirmed_empty_scope_is_valid():
    assert decide(feature()).work.role is Role.PLANNER
    empty = decide(feature(backlog=Backlog(FIRST, ())))
    assert empty.work == Work(Role.ARCHIVIST, FEATURE.number, empty.approval)
    all_superseded = decide(feature(stories={5: story()}, backlog=Backlog(FIRST, (), (5,))))
    assert all_superseded.work == Work(Role.ARCHIVIST, FEATURE.number, all_superseded.approval)
    assert all_superseded.blockers == (Blocker(5, Reason.SUPERSEDED),)


def test_spike_for_approved_design_keeps_its_original_handoff_during_a_revision():
    pending = DesignEvidence(OPEN, False, 1, (), None)
    pin = SpikeResult("refs/heads/spike/101-3", PIN, True)
    done = SpikeEvidence(CLOSED, False, 1, pin, SpikeHandoff(1, PIN))
    result = decide(feature(designs={1: design(), 2: pending}, spikes={3: done}))
    assert result.work == Work(Role.DESIGNER, 2, result.approval)
    assert result.approval.sha == FIRST

    new_spike = SpikeEvidence(OPEN, False, 2)
    result = decide(feature(designs={1: design(), 2: pending}, spikes={3: done, 4: new_spike}))
    assert result.work == Work(Role.SPIKE, 4)
    assert result.approval.sha == FIRST


def test_merge_scope_excludes_drafts_active_work_and_unmet_prerequisites():
    stories = {
        5: story(expected_pr=35, candidates=(pr(35, 5),), prerequisites=(8,)),
        6: story(expected_pr=36, candidates=(pr(36, 6),), active=True),
        7: story(expected_pr=37, candidates=(pr(37, 7, state=PullRequestState.DRAFT),)),
        8: story(),
        9: story(expected_pr=39, candidates=(pr(39, 9),)),
    }
    result = decide(feature(stories=stories, backlog=Backlog(FIRST, tuple(stories))))
    assert result.work == Work(Role.BUILDER, 8, result.approval)
    assert result.merge_candidates == (39,)


def test_current_backlog_does_not_reuse_prior_revision_completion_or_pr():
    designs = {1: design(), 2: design(2, SECOND, 1)}
    for stale in (
        completed(5),
        story(expected_pr=35, candidates=(pr(35, 5),)),
        replace(completed(5), revision=SECOND),  # re-stamping the story cannot reapprove a PR
    ):
        result = decide(feature(designs=designs, stories={5: stale}, backlog=Backlog(SECOND, (5,))))
        assert result.work is None
        assert not result.completed_stories and not result.merge_candidates
        assert Blocker(5, Reason.STALE_STORY) in result.blockers
