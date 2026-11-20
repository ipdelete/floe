"""Decide readiness from a normalized snapshot, without reading or writing GitHub.

The caller supplies complete candidate inventories (empty is known absent, None
is unknown), independently verified commit facts, and trusted per-feature policy.
A work offer is not an admission; merge candidates only pass this scope check.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from .feature_tree import FeatureTree, IssueRef, Phase, UnknownTree, valid_issue_ref


class IssueState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class PullRequestState(StrEnum):
    OPEN = "open"
    DRAFT = "draft"
    CLOSED = "closed"
    MERGED = "merged"


@dataclass(frozen=True)
class Policy:
    maintainer: str
    adr_path: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    repo: str  # base repository
    base: str
    closes: tuple[IssueRef, ...]
    state: PullRequestState
    squash_sha: str | None = None
    squash_verified: bool | None = None  # verified for this PR, including the required trailers
    merged_by: str | None = None
    approved_revision: str | None = None  # independently bound story approval or design predecessor
    head_branch: str | None = None  # recorded same-repo head name, even after ref deletion; None for unverified/fork


@dataclass(frozen=True)
class AdrAtMerge:
    """Git tree entry at the squash, resolved without following symlinks.

    ``changed_in_pr`` is true only for the exact canonical destination in the
    PR diff, not for a rename source or a shadow file.
    """

    path: str
    commit: str
    mode: str
    changed_in_pr: bool


@dataclass(frozen=True)
class SpikeResult:
    branch: str
    commit: str
    retained: bool  # verified commit is still reachable on this retained branch


@dataclass(frozen=True)
class SpikeHandoff:
    design: int
    commit: str


@dataclass(frozen=True)
class SpikeEvidence:
    state: IssueState | None
    active: bool | None
    design: int  # expected destination of the handoff or disposition
    result: SpikeResult | None = None
    handoff: SpikeHandoff | None = None
    disposed_for: int | None = None  # explicit disposition recorded on this design issue


@dataclass(frozen=True)
class DesignEvidence:
    state: IssueState | None
    active: bool | None
    previous_design: int | None  # explicit link; None denotes the initial design
    candidates: tuple[PullRequest, ...] | None
    adr: AdrAtMerge | None


@dataclass(frozen=True)
class StoryEvidence:
    state: IssueState | None
    active: bool | None
    revision: str
    prerequisites: tuple[int, ...]
    expected_pr: int | None  # independently recorded for this revision
    candidates: tuple[PullRequest, ...] | None


@dataclass(frozen=True)
class Backlog:
    """Planner's reconciled partition of all linked stories for one approval.

    An explicitly published empty scope is valid, including when a revision
    supersedes all remaining stories. Missing planning evidence is not a plan.
    """

    revision: str
    expected: tuple[int, ...]
    superseded: tuple[int, ...] = ()  # explicitly disposed of in this reconciliation


@dataclass(frozen=True)
class RevisionRequest:
    """Normalized Builder contradiction journaled on the requesting story."""

    feature: IssueRef
    issue: IssueRef
    revision: str
    attempt: int


@dataclass(frozen=True)
class FeatureEvidence:
    tree: FeatureTree | UnknownTree
    children: Mapping[int, SpikeEvidence | DesignEvidence | StoryEvidence]
    state: IssueState | None
    pending_judgment: bool | None
    closeout_active: bool | None  # active closeout work or an existing closeout candidate
    backlogs: tuple[Backlog, ...] | None = ()  # complete published history; None is unknown
    planner_active: bool | None = False
    # Complete trusted history: () for confirmed absence, None for unknown.
    revision_requests: tuple[RevisionRequest, ...] | None = field(kw_only=True)

    def __post_init__(self):
        object.__setattr__(self, "children", MappingProxyType(dict(self.children)))


class Role(StrEnum):
    SPIKE = "spike"
    DESIGNER = "designer"
    PLANNER = "planner"
    BUILDER = "builder"
    ARCHIVIST = "archivist"


class Reason(StrEnum):
    UNKNOWN_EVIDENCE = "unknown-evidence"
    FEATURE_CLOSED = "feature-closed"
    ACTIVE_WORK = "active-work"
    SPIKE_HANDOFF = "spike-handoff-missing"
    DESIGN_CHAIN = "ambiguous-design-chain"
    AMBIGUOUS_PR = "ambiguous-pr"
    PENDING_DESIGN = "pending-design"
    INVALID_DESIGN_PR = "invalid-design-pr"
    INVALID_ADR = "invalid-adr"
    PENDING_JUDGMENT = "pending-judgment"
    REVISION_REQUESTED = "revision-requested"
    INVALID_BACKLOG = "invalid-backlog"
    SUPERSEDED = "superseded"
    STALE_STORY = "stale-story-evidence"
    STORY_DISCREPANCY = "story-discrepancy"
    UNMET_PREREQUISITE = "unmet-prerequisite"
    EXISTING_PR = "existing-pr"


@dataclass(frozen=True)
class ApprovedDesign:
    issue: int
    pr: int
    sha: str
    adr_path: str


@dataclass(frozen=True)
class Work:
    role: Role
    issue: int
    approval: ApprovedDesign | None = None


@dataclass(frozen=True)
class Blocker:
    issue: int
    reason: Reason
    related: tuple[int, ...] = ()


@dataclass(frozen=True)
class StoryCompletion:
    issue: int
    pr: int
    squash_sha: str


@dataclass(frozen=True)
class StoryMergeCandidate:
    issue: int
    pr: int


@dataclass(frozen=True)
class Decision:
    """Scope results, not authorization to start or merge.

    ``merge_stories`` excludes draft PRs, stories with unmet prerequisites,
    active work, and superseded scope, even if an expected PR already exists.
    """

    approval: ApprovedDesign | None = None
    work: Work | None = None
    blockers: tuple[Blocker, ...] = ()
    completed_stories: tuple[StoryCompletion, ...] = ()
    merge_stories: tuple[StoryMergeCandidate, ...] = ()

    @property
    def merge_candidates(self) -> tuple[int, ...]:
        return tuple(candidate.pr for candidate in self.merge_stories)


def _issue_number(value: object) -> bool:
    return type(value) is int and value > 0


def valid_sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def matches_child_pr(pr: PullRequest, feature: IssueRef, issue: int) -> bool:
    if not isinstance(pr.closes, tuple) or len(pr.closes) != 1:
        return False
    [closing_issue] = pr.closes
    return (_issue_number(pr.number) and pr.repo == feature.repo and pr.base == "master"
            and isinstance(closing_issue, IssueRef) and _issue_number(closing_issue.number)
            and closing_issue == IssueRef(feature.repo, issue))


def _open_candidate(pr: PullRequest) -> bool:
    return ((pr.state is PullRequestState.OPEN or pr.state is PullRequestState.DRAFT)
            and pr.squash_sha is None and pr.merged_by is None
            and (pr.squash_verified is None or pr.squash_verified is False))


def _verified_merge(pr: PullRequest) -> bool:
    return (pr.state is PullRequestState.MERGED and pr.squash_verified is True and valid_sha(pr.squash_sha))


def _known_issue(state: IssueState | None, active: bool | None) -> bool:
    return isinstance(state, IssueState) and type(active) is bool


def _pr_problem(candidates: tuple[PullRequest, ...] | None) -> Reason | None:
    if not isinstance(candidates, tuple) or not all(isinstance(pr, PullRequest) for pr in candidates):
        return Reason.UNKNOWN_EVIDENCE
    if len(candidates) > 1:
        return Reason.AMBIGUOUS_PR
    return None


def _design_order(designs: Mapping[int, DesignEvidence]) -> tuple[int, ...] | None:
    successors = {}
    for issue, design in designs.items():
        previous = design.previous_design
        if previous is not None and (not _issue_number(previous) or previous not in designs):
            return None
        if previous in successors:
            return None
        successors[previous] = issue
    order = []
    issue = successors.get(None)
    while issue is not None:
        order.append(issue)
        issue = successors.get(issue)
    # With one predecessor and at most one successor, cycles can't be reached
    # from the root; any unused nodes therefore make the chain ambiguous.
    return tuple(order) if len(order) == len(designs) else None


def _spike_handed_off(spike: SpikeEvidence) -> bool:
    if _issue_number(spike.disposed_for) and spike.disposed_for == spike.design:
        return True
    pin, handoff = spike.result, spike.handoff
    return (spike.state is IssueState.CLOSED and isinstance(pin, SpikeResult)
            and isinstance(pin.branch, str) and pin.branch.startswith("refs/heads/")
            and pin.branch != "refs/heads/" and valid_sha(pin.commit) and pin.retained is True
            and isinstance(handoff, SpikeHandoff) and _issue_number(handoff.design)
            and handoff.design == spike.design and handoff.commit == pin.commit)


def _spike_decision(
    spikes: Mapping[int, SpikeEvidence], designs: tuple[int, ...], approval: ApprovedDesign | None,
) -> Decision | None:
    work = None
    blockers = []
    for issue, spike in spikes.items():
        if (not _known_issue(spike.state, spike.active) or not _issue_number(spike.design)
                or spike.design not in designs):
            return Decision(approval=approval, blockers=(Blocker(issue, Reason.UNKNOWN_EVIDENCE),))
        if _spike_handed_off(spike):
            continue
        if spike.active:
            blockers.append(Blocker(issue, Reason.ACTIVE_WORK))
        elif spike.state is IssueState.CLOSED:
            blockers.append(Blocker(issue, Reason.SPIKE_HANDOFF))
        elif work is None:
            work = Work(Role.SPIKE, issue)
    if work or blockers:
        return Decision(approval=approval, work=work, blockers=tuple(blockers))
    return None


def _check_design(
    feature: IssueRef, policy: Policy, issue: int, design: DesignEvidence,
) -> ApprovedDesign | Reason:
    if not _known_issue(design.state, design.active):
        return Reason.UNKNOWN_EVIDENCE
    problem = _pr_problem(design.candidates)
    if problem is not None:
        return problem
    if not design.candidates:
        return Reason.PENDING_DESIGN if design.state is IssueState.OPEN else Reason.INVALID_DESIGN_PR
    [pr] = design.candidates
    if not matches_child_pr(pr, feature, issue):
        return Reason.INVALID_DESIGN_PR
    if _open_candidate(pr):
        return Reason.PENDING_DESIGN
    if not _verified_merge(pr) or pr.merged_by != policy.maintainer:
        return Reason.INVALID_DESIGN_PR
    adr = design.adr
    if (not isinstance(adr, AdrAtMerge) or adr.path != policy.adr_path or adr.commit != pr.squash_sha
            or adr.mode not in ("100644", "100755") or adr.changed_in_pr is not True):
        return Reason.INVALID_ADR
    return ApprovedDesign(issue, pr.number, pr.squash_sha, adr.path)


def _story_pr(
    feature: IssueRef, issue: int, story: StoryEvidence, approval: ApprovedDesign,
) -> PullRequest | Reason | None:
    if not _known_issue(story.state, story.active):
        return Reason.UNKNOWN_EVIDENCE
    if story.revision != approval.sha:
        return Reason.STALE_STORY
    problem = _pr_problem(story.candidates)
    if problem is not None:
        return problem
    if not story.candidates:
        if story.expected_pr is not None or story.state is IssueState.CLOSED:
            return Reason.STORY_DISCREPANCY
        return None
    [pr] = story.candidates
    if (not _issue_number(story.expected_pr) or pr.number != story.expected_pr
            or not matches_child_pr(pr, feature, issue)):
        return Reason.STORY_DISCREPANCY
    if pr.approved_revision != approval.sha:
        return Reason.STALE_STORY
    if pr.state is PullRequestState.MERGED:
        return pr if story.state is IssueState.CLOSED and _verified_merge(pr) else Reason.STORY_DISCREPANCY
    if story.state is IssueState.OPEN and _open_candidate(pr):
        return pr
    return Reason.STORY_DISCREPANCY


def valid_issue_numbers(values: object) -> bool:
    return (isinstance(values, tuple) and all(_issue_number(issue) for issue in values)
            and len(set(values)) == len(values))


def _current_backlog(
    backlogs: tuple[Backlog, ...] | None, revisions: tuple[str, ...], issues: tuple[int, ...],
) -> Backlog | Reason | None:
    if not isinstance(backlogs, tuple):
        return Reason.UNKNOWN_EVIDENCE
    by_revision = {}
    for plan in backlogs:
        if (not isinstance(plan, Backlog) or not valid_sha(plan.revision) or plan.revision not in revisions
                or plan.revision in by_revision or not valid_issue_numbers(plan.expected)
                or not valid_issue_numbers(plan.superseded) or set(plan.expected).intersection(plan.superseded)):
            return Reason.INVALID_BACKLOG
        by_revision[plan.revision] = plan

    previous_expected = set()
    known_stories = set(issues)
    for revision in revisions:
        plan = by_revision.get(revision)
        if plan is None:
            continue
        expected, superseded = set(plan.expected), set(plan.superseded)
        if not previous_expected.issubset(expected | superseded):
            return Reason.INVALID_BACKLOG
        if revision == revisions[-1]:
            if (not expected.issubset(issues) or not superseded.issubset(known_stories)
                    or not set(issues).issubset(expected | superseded)):
                return Reason.INVALID_BACKLOG
            return plan
        previous_expected = expected
        known_stories.update(expected, superseded)
    return None  # confirmed that Planner has not reconciled this revision


def _decide_stories(
    feature: IssueRef, evidence: FeatureEvidence, approval: ApprovedDesign, backlog: Backlog,
    ordered: tuple[int, ...], design_prs: set[int],
) -> Decision:
    blockers = []
    completed = {}
    candidates = {}
    stories = {}
    seen_prs = set(design_prs)
    for issue in ordered:
        if issue in backlog.superseded:
            continue
        story = evidence.children.get(issue)
        if not isinstance(story, StoryEvidence):
            blockers.append(Blocker(issue, Reason.UNKNOWN_EVIDENCE))
            continue
        pr = _story_pr(feature, issue, story, approval)
        if isinstance(pr, Reason):
            blockers.append(Blocker(issue, pr))
        elif (not valid_issue_numbers(story.prerequisites) or issue in story.prerequisites
              or not set(story.prerequisites).issubset(ordered)):
            blockers.append(Blocker(issue, Reason.INVALID_BACKLOG))
        elif pr is not None and pr.number in seen_prs:
            blockers.append(Blocker(issue, Reason.AMBIGUOUS_PR))
        else:
            stories[issue] = story
            if pr is not None:
                seen_prs.add(pr.number)
                if pr.state is PullRequestState.MERGED:
                    completed[issue] = StoryCompletion(issue, pr.number, pr.squash_sha)
                else:
                    candidates[issue] = pr

    if blockers:
        return Decision(approval=approval, blockers=tuple(blockers))

    work = None
    merge_stories = []
    blockers.extend(Blocker(issue, Reason.SUPERSEDED) for issue in backlog.superseded if issue not in ordered)
    for issue in ordered:
        if issue in backlog.superseded:
            blockers.append(Blocker(issue, Reason.SUPERSEDED))
            continue
        if issue in completed:
            continue
        story = stories[issue]
        unmet = tuple(prerequisite for prerequisite in story.prerequisites if prerequisite not in completed)
        if unmet:
            blockers.append(Blocker(issue, Reason.UNMET_PREREQUISITE, unmet))
        elif story.active:
            blockers.append(Blocker(issue, Reason.ACTIVE_WORK))
        elif issue in candidates:
            blockers.append(Blocker(issue, Reason.EXISTING_PR, (candidates[issue].number,)))
            if candidates[issue].state is PullRequestState.OPEN:
                merge_stories.append(StoryMergeCandidate(issue, candidates[issue].number))
        elif work is None:
            work = Work(Role.BUILDER, issue, approval)

    if completed.keys() == set(backlog.expected):
        if type(evidence.closeout_active) is not bool:
            blockers.append(Blocker(feature.number, Reason.UNKNOWN_EVIDENCE))
        elif evidence.closeout_active:
            blockers.append(Blocker(feature.number, Reason.ACTIVE_WORK))
        else:
            work = Work(Role.ARCHIVIST, feature.number, approval)
    return Decision(approval, work, tuple(blockers), tuple(completed.values()), tuple(merge_stories))


def _revision_blockers(
    feature: IssueRef, requests: tuple[RevisionRequest, ...] | None,
    revisions: list[str], stories: list[int],
) -> tuple[Blocker, ...]:
    if not isinstance(requests, tuple):
        return (Blocker(feature.number, Reason.UNKNOWN_EVIDENCE),)
    requesting = set()
    for request in requests:
        if (not isinstance(request, RevisionRequest) or not valid_issue_ref(request.feature)
                or request.feature != feature or not valid_issue_ref(request.issue)
                or request.issue.repo != feature.repo or request.issue.number == feature.number
                or not _issue_number(request.attempt) or not valid_sha(request.revision)
                or request.revision not in revisions):
            return (Blocker(feature.number, Reason.UNKNOWN_EVIDENCE),)
        if request.revision == revisions[-1]:
            requesting.add(request.issue.number)
    # A missing edge for a current request might reflect an unlink, not resolution.
    if not requesting.issubset(stories):
        return (Blocker(feature.number, Reason.UNKNOWN_EVIDENCE),)
    return tuple(Blocker(issue, Reason.REVISION_REQUESTED) for issue in stories if issue in requesting)


def decide_feature(feature: IssueRef, policy: Policy, evidence: FeatureEvidence) -> Decision:
    """Offer the next role in one feature without letting ambiguity escape it.

    Only actual ordered children contribute work. Revision links, not that
    order, determine which approval is current. The caller still rechecks
    current evidence at admission and merge time.
    """
    if (not isinstance(evidence.tree, FeatureTree) or not isinstance(evidence.state, IssueState)
            or type(evidence.pending_judgment) is not bool):
        return Decision(blockers=(Blocker(feature.number, Reason.UNKNOWN_EVIDENCE),))
    if evidence.state is IssueState.CLOSED:
        return Decision(blockers=(Blocker(feature.number, Reason.FEATURE_CLOSED),))

    spikes = {}
    designs = {}
    stories = []
    for issue, phase in evidence.tree.phases.items():
        if not _issue_number(issue) or issue == feature.number:
            return Decision(blockers=(Blocker(feature.number, Reason.UNKNOWN_EVIDENCE),))
        child = evidence.children.get(issue)
        if phase is Phase.SPIKE and isinstance(child, SpikeEvidence):
            spikes[issue] = child
        elif phase is Phase.DESIGN and isinstance(child, DesignEvidence):
            designs[issue] = child
        elif phase is Phase.STORY:
            stories.append(issue)
        else:
            return Decision(blockers=(Blocker(issue, Reason.UNKNOWN_EVIDENCE),))

    order = _design_order(designs)
    if not order:
        return Decision(blockers=(Blocker(feature.number, Reason.DESIGN_CHAIN),))

    approval = None
    revisions = []
    design_prs = set()
    pending_design = None
    for issue in order:
        design = designs[issue]
        result = _check_design(feature, policy, issue, design)
        if isinstance(result, ApprovedDesign):
            if result.sha in revisions or result.pr in design_prs:
                return Decision(approval=approval, blockers=(Blocker(issue, Reason.DESIGN_CHAIN),))
            approval = result
            revisions.append(result.sha)
            design_prs.add(result.pr)
            continue
        # Only the tip of a resolved chain can be waiting for design approval.
        if result is not Reason.PENDING_DESIGN or issue != order[-1]:
            return Decision(approval=approval, blockers=(Blocker(issue, result),))
        pending_design = issue

    # Resolve spike handoffs before offering Designer or advancing beyond an
    # approval. Each pin still belongs to its original design, not today's tip.
    spike_decision = _spike_decision(spikes, order, approval)
    if spike_decision is not None:
        return spike_decision
    if pending_design is not None:
        design = designs[pending_design]
        work = None
        if design.candidates == () and not design.active:
            work = Work(Role.DESIGNER, pending_design, approval)
        return Decision(approval, work, (Blocker(pending_design, Reason.PENDING_DESIGN),))

    if evidence.pending_judgment:
        return Decision(approval=approval, blockers=(Blocker(feature.number, Reason.PENDING_JUDGMENT),))
    blockers = _revision_blockers(feature, evidence.revision_requests, revisions, stories)
    if blockers:
        return Decision(approval=approval, blockers=blockers)
    ordered_stories = tuple(stories)
    backlog = _current_backlog(evidence.backlogs, tuple(revisions), ordered_stories)
    if isinstance(backlog, Reason):
        return Decision(approval=approval, blockers=(Blocker(feature.number, backlog),))
    if backlog is None:
        if type(evidence.planner_active) is not bool:
            return Decision(approval=approval, blockers=(Blocker(feature.number, Reason.UNKNOWN_EVIDENCE),))
        if evidence.planner_active:
            return Decision(approval=approval, blockers=(Blocker(feature.number, Reason.ACTIVE_WORK),))
        return Decision(approval, Work(Role.PLANNER, feature.number, approval))
    return _decide_stories(feature, evidence, approval, backlog, ordered_stories, design_prs)
