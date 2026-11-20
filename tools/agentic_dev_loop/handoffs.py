"""Role handoffs over trusted, normalized fixture evidence. No side effects.

These contracts neither authorize GitHub writes nor reserve a worker. Issue
prose and rendered notes are not evidence of parentage, approval or liveness.
"""

import re
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path, PurePosixPath

from .feature_tree import FeatureTree, IssueRef, Phase, valid_issue_ref
from .launcher import AttemptKey, Reservation
from .readiness import (
    ApprovedDesign, Backlog, PullRequest, PullRequestState, RevisionRequest, Role,
    SpikeHandoff, SpikeResult, Work, matches_child_pr, valid_issue_numbers, valid_sha,
)


class HandoffError(ValueError):
    """The supplied handoff cannot be tied to the expected evidence."""


_PHASE = {Role.SPIKE: Phase.SPIKE, Role.DESIGNER: Phase.DESIGN, Role.BUILDER: Phase.STORY}
_BRANCH = {Role.SPIKE: "spike", Role.DESIGNER: "design", Role.BUILDER: "feature", Role.ARCHIVIST: "closeout"}


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _slug(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is not None


def _feature_tag(value: IssueRef, slug: str) -> str:
    if (not valid_issue_ref(value) or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*", value.repo)
            or not _slug(slug)):
        raise HandoffError("expected a canonical feature issue and slug")
    return f"{value.number}-{slug}"


def _check_id(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
        raise HandoffError("expected a canonical session or run identifier")


def _linked(tree: FeatureTree, feature: IssueRef, issue: IssueRef, phase: Phase) -> bool:
    return (isinstance(tree, FeatureTree) and valid_issue_ref(issue) and issue.repo == feature.repo
            and issue != feature and tree.phases.get(issue.number) is phase)


def _repo_url(feature: IssueRef) -> str:
    return f"https://github.com/{feature.repo}"


@dataclass(frozen=True, slots=True)
class IssueNote:
    issue: IssueRef
    body: str


def intake_note(feature: IssueRef, slug: str, tree: FeatureTree, session_id: str, *,
                requirements_comment: int) -> IssueNote:
    """Render on the feature issue; the requirements and decision seed stay there."""
    tag = _feature_tag(feature, slug)
    _check_id(session_id)
    if not isinstance(tree, FeatureTree) or not _positive(requirements_comment):
        raise HandoffError("expected actual child edges and a requirements comment on the feature")
    url = f"{_repo_url(feature)}/issues"
    lines = [f"Feature: #{feature.number}", "Role: intake", f"{tag} — intake #{feature.number}",
             f"Session: {session_id}", f"Requirements: {url}/{feature.number}#issuecomment-{requirements_comment}"]
    for number, phase in tree.phases.items():
        if not _positive(number) or number == feature.number:
            raise HandoffError("invalid child edge in feature inventory")
        if phase is Phase.SPIKE or phase is Phase.DESIGN:
            lines.append(f"{phase.value.capitalize()}: {url}/{number}")
    return IssueNote(feature, "\n".join(lines))


@dataclass(frozen=True, slots=True)
class RoleInput:
    feature: IssueRef
    slug: str
    issue: IssueRef
    role: Role
    attempt: int
    approval: ApprovedDesign | None

    def __post_init__(self) -> None:
        _feature_tag(self.feature, self.slug)
        if (not valid_issue_ref(self.issue) or self.issue.repo != self.feature.repo
                or not isinstance(self.role, Role) or not _positive(self.attempt)
                or (self.role in _PHASE) == (self.issue == self.feature)):
            raise HandoffError("invalid role or attempt target")
        approval = self.approval
        if approval is not None:
            if (not isinstance(approval, ApprovedDesign) or not valid_sha(approval.sha)
                    or not _positive(approval.pr) or not _positive(approval.issue)
                    or approval.issue in (self.feature.number, self.issue.number)
                    or not _adr_path(approval.adr_path) or self.role is Role.SPIKE):
                raise HandoffError("invalid approved design binding")
        elif self.role in (Role.PLANNER, Role.BUILDER, Role.ARCHIVIST):
            raise HandoffError("role requires an approved design revision")

    @property
    def tag(self) -> str:
        return f"{self.feature.number}-{self.slug}"

    @property
    def _suffix(self) -> str:
        return "" if self.attempt == 1 else f"-attempt-{self.attempt}"

    @property
    def branch(self) -> str | None:
        prefix = _BRANCH.get(self.role)
        return f"{prefix}/{self.tag}-{self.issue.number}{self._suffix}" if prefix else None

    @property
    def session_name(self) -> str:
        label = _PHASE[self.role].value if self.role in _PHASE else self.role.value
        suffix = "" if self.attempt == 1 else f" — attempt {self.attempt}"
        return f"{self.tag} — {label} #{self.issue.number}{suffix}"

    def worktree(self, private_root: Path) -> Path:
        """Name the private checkout; its allocation and ownership are the caller's job."""
        return private_root / self.feature.repo / self.tag / f"{self.role}-{self.issue.number}{self._suffix}"


def role_input(feature: IssueRef, slug: str, tree: FeatureTree, work: Work, *, attempt: int = 1) -> RoleInput:
    """Build from the readiness offer and actual child inventory, not from issue text."""
    if not valid_issue_ref(feature) or not isinstance(work, Work) or not isinstance(tree, FeatureTree):
        raise HandoffError("expected a role offer and an attempt on a known feature tree")
    inputs = RoleInput(feature, slug, IssueRef(feature.repo, work.issue), work.role, attempt, work.approval)
    if inputs.role in _PHASE and not _linked(tree, feature, inputs.issue, _PHASE[inputs.role]):
        raise HandoffError("role target is not a linked child of the expected phase")
    approval = inputs.approval
    if approval is not None and not _linked(tree, feature, IssueRef(feature.repo, approval.issue), Phase.DESIGN):
        raise HandoffError("approved design is not a linked child")
    return inputs


def _adr_path(path: object) -> bool:
    return (isinstance(path, str) and bool(path) and not PurePosixPath(path).is_absolute()
            and re.fullmatch(r"[A-Za-z0-9._/-]+", path) is not None
            and ".." not in PurePosixPath(path).parts and PurePosixPath(path).as_posix() == path
            and PurePosixPath(path).name == "design.md")


def issue_note(inputs: RoleInput, key: AttemptKey, reservation: Reservation) -> IssueNote:
    """Mirror the IDs only. The caller maps this role input to its stable GitHub IDs."""
    if (not isinstance(key, AttemptKey) or key.role is not inputs.role or key.attempt != inputs.attempt
            or not all(_positive(value) for value in (key.repo_id, key.feature_id, key.issue_id, key.attempt))
            or not isinstance(reservation, Reservation) or reservation.key != key):
        raise HandoffError("reservation does not match the attempt")
    _check_id(reservation.run_id)
    _check_id(reservation.session_id)
    lines = [f"Feature: #{inputs.feature.number}", f"Role: {inputs.role}", f"Attempt: {inputs.attempt}",
             f"Name: {inputs.session_name}"]
    if inputs.approval:
        lines.append(f"Revision: {inputs.approval.sha}")
    lines.extend((f"Run: {reservation.run_id}", f"Session: {reservation.session_id}"))
    return IssueNote(inputs.issue, "\n".join(lines))


def development_matches(inputs: RoleInput, ref: str | None) -> bool:
    """Check an observed Development ref for this issue; a match says nothing about admission."""
    return inputs.branch is not None and ref == f"refs/heads/{inputs.branch}"


@dataclass(frozen=True, slots=True)
class SpikeDelivery:
    source: RoleInput
    result: SpikeResult
    handoff: SpikeHandoff  # record on this design issue before closing the spike
    commit_url: str


def spike_delivery(inputs: RoleInput, tree: FeatureTree, design: IssueRef,
                   development_ref: str, pin: SpikeResult) -> SpikeDelivery:
    if (inputs.role is not Role.SPIKE or not _linked(tree, inputs.feature, inputs.issue, Phase.SPIKE)
            or not _linked(tree, inputs.feature, design, Phase.DESIGN)
            or not development_matches(inputs, development_ref) or not isinstance(pin, SpikeResult)
            or pin.branch != development_ref or pin.retained is not True or not valid_sha(pin.commit)):
        raise HandoffError("spike requires actual edges and a verified pin on its retained Development branch")
    return SpikeDelivery(inputs, pin, SpikeHandoff(design.number, pin.commit),
                         f"{_repo_url(inputs.feature)}/commit/{pin.commit}")


def revision_request(inputs: RoleInput) -> RevisionRequest:
    """Report a contradiction on the story; do not amend the design in the story PR."""
    if inputs.role is not Role.BUILDER or inputs.approval is None:
        raise HandoffError("a Builder revision request requires an approved revision")
    return RevisionRequest(inputs.feature, inputs.issue, inputs.approval.sha, inputs.attempt)


def pr_body(inputs: RoleInput) -> str:
    """Render the linkage header; purpose-written prose is separate from the handoff."""
    if inputs.role not in (Role.BUILDER, Role.DESIGNER):
        raise HandoffError("expected a story or design PR")
    lines = [f"Closes #{inputs.issue.number}", f"Refs #{inputs.feature.number}",
             f"Role: {inputs.role}", f"Attempt: {inputs.attempt}"]
    if inputs.approval:
        lines.append(f"Revision: {inputs.approval.sha}")
    return "\n".join(lines)


def valid_work_commit(inputs: RoleInput, message: str) -> bool:
    refs = [f"Refs: #{inputs.feature.number}"]
    if inputs.issue != inputs.feature:
        refs.append(f"Refs: #{inputs.issue.number}")
    return isinstance(message, str) and message.rstrip("\n").endswith("\n\n" + "\n".join(refs))


@dataclass(frozen=True, slots=True)
class PRTrace:
    issue: IssueRef
    pr_url: str
    squash_url: str | None


def pr_trace(inputs: RoleInput, pr: PullRequest) -> PRTrace:
    """Use observed closing links and verified squash evidence, not branch URLs."""
    revision = inputs.approval.sha if inputs.approval is not None else None
    if (inputs.role not in (Role.BUILDER, Role.DESIGNER) or not isinstance(pr, PullRequest)
            or not matches_child_pr(pr, inputs.feature, inputs.issue.number) or pr.head_branch != inputs.branch
            or pr.approved_revision != revision
            or not isinstance(pr.state, PullRequestState)):
        raise HandoffError("PR does not match the expected child and revision")
    url = _repo_url(inputs.feature)
    squash = None
    if pr.state is PullRequestState.MERGED:
        if pr.squash_verified is not True or not valid_sha(pr.squash_sha):
            raise HandoffError("missing verified squash evidence")
        squash = f"{url}/commit/{pr.squash_sha}"
    return PRTrace(inputs.issue, f"{url}/pull/{pr.number}", squash)


@dataclass(frozen=True, slots=True)
class StorySpec:
    """A stable marker key and the Planner's purpose-written requirements."""

    key: str
    title: str
    checks: tuple[str, ...]
    prerequisites: tuple[str, ...] = ()  # stable keys, not sub-issue priority


@dataclass(frozen=True, slots=True)
class StoryRecord:
    issue: IssueRef
    spec: StorySpec
    revision: str
    prerequisites: tuple[IssueRef, ...]  # actual issue links observed on this story


@dataclass(frozen=True, slots=True)
class Plan:
    create: tuple[StorySpec, ...]
    rebind: tuple[IssueRef, ...]
    backlog: Backlog | None  # publish only after all desired effects were reread


def _valid_spec(spec: object) -> bool:
    return (isinstance(spec, StorySpec) and _slug(spec.key)
            and isinstance(spec.title, str) and spec.title.startswith("Story:") and bool(spec.title[6:].strip())
            and isinstance(spec.checks, tuple) and bool(spec.checks)
            and all(isinstance(check, str) and check.strip() for check in spec.checks)
            and isinstance(spec.prerequisites, tuple) and all(_slug(key) for key in spec.prerequisites)
            and len(set(spec.prerequisites)) == len(spec.prerequisites))


def reconcile_plan(inputs: RoleInput, current: ApprovedDesign, tree: FeatureTree,
                   desired: tuple[StorySpec, ...], observed: tuple[StoryRecord, ...] | None, *,
                   previous: Backlog | None = None, superseded: tuple[int, ...] = ()) -> Plan:
    """Match complete marker evidence with actual edges and publish no speculative backlog.

    Marker reads include unlinked matches. Recover an unlinked creation via the
    writer before retrying here; absence from the child list is not permission
    to create. The writer handles operation IDs and uncertain effects.
    ``previous`` is the last verified backlog before this revision, if any.
    """
    if (inputs.role is not Role.PLANNER or inputs.approval is None or current != inputs.approval
            or not _linked(tree, inputs.feature, IssueRef(inputs.feature.repo, current.issue), Phase.DESIGN)
            or not isinstance(observed, tuple)):
        raise HandoffError("expected the current approved design and complete story inventory")
    if not isinstance(desired, tuple) or not all(_valid_spec(spec) for spec in desired):
        raise HandoffError("expected a complete set of valid story specifications")
    specs = {spec.key: spec for spec in desired}
    if len(specs) != len(desired):
        raise HandoffError("duplicate planned story marker")
    graph = {key: spec.prerequisites for key, spec in specs.items()}
    if any(not set(keys).issubset(graph) for keys in graph.values()):
        raise HandoffError("prerequisites must be in the planned scope")
    try:
        order = tuple(TopologicalSorter(graph).static_order())
    except CycleError:
        raise HandoffError("cyclic story prerequisites") from None

    if not valid_issue_numbers(superseded):
        raise HandoffError("invalid superseded story disposition")
    older = set()
    if previous is not None:
        if (not isinstance(previous, Backlog) or not valid_sha(previous.revision) or previous.revision == current.sha
                or not valid_issue_numbers(previous.expected) or not valid_issue_numbers(previous.superseded)
                or set(previous.expected).intersection(previous.superseded)):
            raise HandoffError("invalid previous backlog")
        older.update(previous.expected, previous.superseded)
    retired = set(superseded)
    linked_stories = {number for number, phase in tree.phases.items() if phase is Phase.STORY}
    if (not retired.issubset(linked_stories | older)
            or (previous is not None and not set(previous.expected).issubset(linked_stories | retired))):
        raise HandoffError("previous or superseded scope does not match the observed story edges")
    by_key = {}
    by_number = set()
    for record in observed:
        if (not isinstance(record, StoryRecord) or not _linked(tree, inputs.feature, record.issue, Phase.STORY)
                or not valid_sha(record.revision) or record.issue.number in by_number
                or not _valid_spec(record.spec) or record.spec.key in by_key):
            raise HandoffError("uncertain or conflicting story marker or parent edge")
        by_key[record.spec.key] = record
        by_number.add(record.issue.number)
    if not linked_stories.issubset(by_number | retired):
        raise HandoffError("linked stories must be inventoried or explicitly superseded")

    create = []
    rebind = []
    expected = []
    for key in order:
        spec, record = specs[key], by_key.get(key)
        if record is None:
            create.append(spec)
            continue
        prerequisites = tuple(by_key[dep].issue for dep in spec.prerequisites if dep in by_key)
        if (record.spec != spec or record.issue.number in retired or len(prerequisites) != len(spec.prerequisites)
                or not isinstance(record.prerequisites, tuple) or not all(map(valid_issue_ref, record.prerequisites))
                or record.prerequisites != prerequisites):
            raise HandoffError("existing story does not match its planned marker, scope or prerequisite links")
        expected.append(record.issue.number)
        if record.revision != current.sha:
            rebind.append(record.issue)
    if by_number - set(expected) - retired:
        raise HandoffError("unmatched story requires an explicit disposition")
    backlog = None if create or rebind else Backlog(current.sha, tuple(expected), superseded)
    return Plan(tuple(create), tuple(rebind), backlog)
