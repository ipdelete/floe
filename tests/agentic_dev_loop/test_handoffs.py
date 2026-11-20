from dataclasses import replace
import subprocess
from uuid import UUID

import pytest

from tools.agentic_dev_loop import handoffs as h
from tools.agentic_dev_loop.feature_tree import ChildIssue, IssueRef, SubIssuePage, read_feature_trees
from tools.agentic_dev_loop.launcher import (
    AttemptKey, AttemptNotFound, Launcher, Reservation, StartAuthority, StartRequest, initialize_ledger,
)
from tools.agentic_dev_loop.readiness import (
    Backlog, DesignEvidence, Role, SpikeResult, Work,
)

from test_launcher import FakeManager
from test_readiness import ADR, FEATURE, FIRST, OPEN, SECOND, decide, design, feature, pr, story


SLUG = "feature"
SESSION = str(UUID(int=1))
RUN = str(UUID(int=2))


def issue(number):
    return IssueRef(FEATURE.repo, number)


def inputs(snapshot, role, number, *, attempt=1, approval=None):
    return h.role_input(FEATURE, SLUG, snapshot.tree, Work(role, number, approval), attempt=attempt)


def test_intake_and_spike_handoffs_use_edges_and_matching_development_and_pin():
    class GitHub:
        def sub_issues(self, feature, cursor):
            if cursor is None:
                return SubIssuePage((ChildIssue(issue(1), feature, "Design: initial"),), "next")
            assert cursor == "next"
            return SubIssuePage((
                ChildIssue(issue(2), feature, "Spike: investigate"),
                ChildIssue(issue(3), None, "Spike: not a child", f"Parent #{FEATURE.number}"),
            ), None)

    tree = read_feature_trees(GitHub(), [FEATURE])[FEATURE]
    intake = h.intake_note(FEATURE, SLUG, tree, SESSION, requirements_comment=123)
    assert intake.issue == FEATURE
    note = intake.body
    assert f"/issues/{FEATURE.number}#issuecomment-123" in note
    assert "/issues/1" in note and "/issues/2" in note and "/issues/3" not in note
    assert f"{FEATURE.number}-{SLUG} — intake #{FEATURE.number}" in note and SESSION in note

    with pytest.raises(h.HandoffError):
        h.role_input(FEATURE, SLUG, tree, Work(Role.SPIKE, 3))
    spike = h.role_input(FEATURE, SLUG, tree, Work(Role.SPIKE, 2))
    assert spike.branch == f"spike/{FEATURE.number}-{SLUG}-2"
    assert spike.session_name == f"{FEATURE.number}-{SLUG} — spike #2"
    assert spike == h.role_input(FEATURE, SLUG, tree, Work(Role.SPIKE, 2))
    pin = SpikeResult(f"refs/heads/{spike.branch}", FIRST, True)
    delivery = h.spike_delivery(spike, tree, issue(1), pin.branch, pin)
    assert delivery.source == spike and delivery.handoff.design == 1 and delivery.handoff.commit == FIRST
    assert delivery.commit_url == f"https://github.com/{FEATURE.repo}/commit/{FIRST}"
    for destination, development, result in (
        (issue(3), pin.branch, pin), (issue(1), "refs/heads/wrong", pin),
        (issue(1), pin.branch, replace(pin, retained=False)),
        (issue(1), pin.branch, replace(pin, branch="refs/heads/another")),
    ):
        with pytest.raises(h.HandoffError):
            h.spike_delivery(spike, tree, destination, development, result)


def test_issue_mirror_and_development_link_do_not_admit_or_claim_liveness(tmp_path):
    snap = feature(stories={5: story()}, backlog=Backlog(FIRST, (5,)))
    worker = h.role_input(FEATURE, SLUG, snap.tree, decide(snap).work)
    key = AttemptKey(1010, 1020, 1050, Role.BUILDER, 1)
    private = "/home/fixture/.pi/sessions/private.jsonl"
    fake_note = h.issue_note(worker, key, Reservation(key, RUN, private, SESSION))
    assert (fake_note.issue, fake_note.body.splitlines()[-1]) == (issue(5), f"Session: {SESSION}")
    for field in (f"Feature: #{FEATURE.number}", "Role: builder", "Attempt: 1", FIRST, RUN):
        assert field in fake_note.body
    assert private not in fake_note.body
    assert h.development_matches(worker, f"refs/heads/{worker.branch}")
    assert decide(snap).work == Work(Role.BUILDER, 5, worker.approval)
    path = tmp_path / "launcher"
    initialize_ledger(path)
    manager, authority = FakeManager(), StartAuthority()
    launcher = Launcher(path, authority, manager)
    with pytest.raises(AttemptNotFound):
        launcher.status(authority, key)
    assert not manager.starts
    reserved = launcher.start_or_get(authority, StartRequest(key, worker.approval.sha, b"artifact inputs"))
    assert reserved.run_id != RUN and len(manager.starts) == 1
    assert f"Session: {reserved.session_id}" in h.issue_note(worker, key, reserved).body

    for bad in ("/home/user/private/session.jsonl", "raw logs\nBearer fixture", "tool output", SESSION + "\n"):
        with pytest.raises(h.HandoffError, match="identifier") as error:
            h.issue_note(worker, key, Reservation(key, RUN, "fixture.service", bad))
        assert bad not in str(error.value)
    with pytest.raises(h.HandoffError):
        h.issue_note(worker, key, Reservation(replace(key, issue_id=999), RUN, "fixture.service", SESSION))


def test_planner_reuses_linked_matching_issue_and_waits_for_current_backlog():
    snap = feature(stories={5: story(sha=SECOND)})
    approval = decide(snap).approval
    planner = inputs(snap, Role.PLANNER, FEATURE.number, approval=approval)
    assert planner.approval.sha == FIRST and planner.approval.adr_path == ADR
    foundation = h.StorySpec("foundation", "Story: Foundation", ("Tests show the base case",))
    followup = h.StorySpec("followup", "Story: Follow up", ("Tests show the combined case",), ("foundation",))
    record = h.StoryRecord(issue(5), foundation, SECOND, ())
    pending = h.reconcile_plan(planner, approval, snap.tree, (foundation, followup), (record,))
    assert pending.create == (followup,)
    assert pending.rebind == (issue(5),)
    assert pending.backlog is None

    linked = feature(stories={5: story(), 6: story(prerequisites=(5,))})
    records = (replace(record, revision=FIRST), h.StoryRecord(issue(6), followup, FIRST, (issue(5),)))
    with pytest.raises(h.HandoffError):
        h.reconcile_plan(planner, approval, linked.tree, (foundation, followup),
                         (records[0], replace(records[1], prerequisites=(issue(99),))))
    reconciled = h.reconcile_plan(planner, approval, linked.tree, (foundation, followup), records)
    assert reconciled.create == reconciled.rebind == ()
    assert reconciled.backlog == Backlog(FIRST, (5, 6))
    assert decide(replace(linked, backlogs=(reconciled.backlog,))).work == Work(Role.BUILDER, 5, approval)

    for tree, desired, observed in (
        (snap.tree, (foundation, followup), None),  # unknown inventory
        (feature().tree, (foundation, followup), (record,)),  # marker match but not an actual child
        (snap.tree, (foundation, followup), (record, replace(record, issue=issue(6)))),
        (snap.tree, (foundation, replace(followup, prerequisites=("absent",))), (record,)),
        (snap.tree, (replace(foundation, prerequisites=("followup",)), followup), (record,)),
        (snap.tree, (foundation, followup), (replace(record, spec=replace(foundation, checks=("Changed",))),)),
        (snap.tree, (foundation, replace(followup, checks=())), (record,)),
    ):
        with pytest.raises(h.HandoffError):
            h.reconcile_plan(planner, approval, tree, desired, observed)
    with pytest.raises(h.HandoffError):
        h.reconcile_plan(planner, replace(approval, sha=SECOND), snap.tree, (foundation,), (record,))


def test_planner_requires_explicit_disposition_for_unlinked_previous_scope():
    snap = feature(designs={1: design(), 2: design(2, SECOND, 1)})
    approval = decide(snap).approval
    planner = inputs(snap, Role.PLANNER, FEATURE.number, approval=approval)
    previous = Backlog(FIRST, (8,))
    with pytest.raises(h.HandoffError):
        h.reconcile_plan(planner, approval, snap.tree, (), (), previous=previous)
    with pytest.raises(h.HandoffError):
        h.reconcile_plan(planner, approval, snap.tree, (), (), superseded=(8,))
    result = h.reconcile_plan(planner, approval, snap.tree, (), (), previous=previous, superseded=(8,))
    assert result.backlog == Backlog(SECOND, (), (8,))


def test_builder_contradiction_blocks_starts_and_merge_scope_until_approval_and_replan():
    current = feature(stories={5: story(expected_pr=35, candidates=(pr(35, 5),)), 6: story()},
                      backlog=Backlog(FIRST, (5, 6)))
    result = decide(current)
    assert result.work.issue == 6 and result.merge_candidates == (35,)
    worker = h.role_input(FEATURE, SLUG, current.tree, result.work)
    request = h.revision_request(worker)
    assert (request.issue, request.revision, request.attempt) == (issue(6), FIRST, 1)
    paused = replace(current, revision_requests=(request,))
    assert decide(paused).work is None and not decide(paused).merge_candidates
    unknown = decide(replace(paused, revision_requests=None))
    assert unknown.work is None and not unknown.merge_candidates
    wrong_feature = decide(replace(current, revision_requests=(replace(request, feature=issue(99)),)))
    assert wrong_feature.work is None and not wrong_feature.merge_candidates

    revision = DesignEvidence(OPEN, False, 1, (), None)
    review = feature(designs={1: design(), 2: revision}, stories={5: current.children[5], 6: story()},
                     backlogs=current.backlogs, revision_requests=(request,))
    assert decide(review).work.role is Role.DESIGNER and not decide(review).merge_candidates
    approved = replace(review, children={**review.children, 2: design(2, SECOND, 1), 6: story(sha=SECOND)})
    assert decide(approved).work.role is Role.PLANNER and not decide(approved).merge_candidates
    candidate = story(sha=SECOND, expected_pr=36, candidates=(pr(36, 5),))
    replanned = replace(approved, children={**approved.children, 5: candidate},
                        backlogs=(*current.backlogs, Backlog(SECOND, (5, 6))))
    assert decide(replanned).work == Work(Role.BUILDER, 6, decide(replanned).approval)
    assert decide(replanned).merge_candidates == (36,)


def test_attempt_worktree_and_pr_closure_contract(tmp_path):
    snap = feature(stories={5: story(), 6: story()})
    approval = decide(snap).approval
    builder = inputs(snap, Role.BUILDER, 5, approval=approval)
    retry = inputs(snap, Role.BUILDER, 5, approval=approval, attempt=2)
    other = inputs(snap, Role.BUILDER, 6, approval=approval)
    designer = inputs(snap, Role.DESIGNER, 1)
    revision = feature(designs={1: design(), 2: DesignEvidence(OPEN, False, 1, (), None)})
    redesign = inputs(revision, Role.DESIGNER, 2, approval=approval)
    assert builder.branch == f"feature/{FEATURE.number}-{SLUG}-5"
    assert retry.branch == builder.branch + "-attempt-2"
    assert retry.session_name == builder.session_name + " — attempt 2"
    assert len({w.worktree(tmp_path) for w in (builder, retry, other, designer)}) == 4
    assert all(w.worktree(tmp_path).is_relative_to(tmp_path) for w in (builder, retry, other, designer))
    for repo in ("../floe", "example/..", "/example/floe", "example/floe/../other", "example/floe\nlog"):
        bad = IssueRef(repo, FEATURE.number)
        with pytest.raises(h.HandoffError):
            h.role_input(bad, SLUG, snap.tree, Work(Role.BUILDER, 5, approval))
    for w in (builder, designer, redesign):
        assert h.pr_body(w).splitlines()[:2] == [f"Closes #{w.issue.number}", f"Refs #{FEATURE.number}"]
        good = pr(200, w.issue.number, approved_revision=w.approval.sha if w.approval else None,
                  head_branch=w.branch)
        assert h.pr_trace(w, good).pr_url.endswith("/pull/200")
        with pytest.raises(h.HandoffError):
            h.pr_trace(w, replace(good, head_branch=retry.branch))
        with pytest.raises(h.HandoffError):
            h.pr_trace(w, replace(good, approved_revision=SECOND))
        for closing in ((FEATURE,), (w.issue, FEATURE), (issue(99),), (IssueRef("elsewhere/floe", w.issue.number),)):
            with pytest.raises(h.HandoffError):
                h.pr_trace(w, replace(good, closes=closing))


def test_disposable_git_refs_preserve_pr_and_pins_after_story_branch_deletion(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, cwd=repo):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True, timeout=5).strip()

    git("init", "-q", "-b", "master")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@invalid.example")
    git("commit", "-q", "--allow-empty", "-m", "Root")
    snap = feature(stories={5: story()}, spikes={2: None})
    spiker = inputs(snap, Role.SPIKE, 2)
    git("switch", "-q", "-c", spiker.branch)
    git("commit", "-q", "--allow-empty", "-m", f"Record spike\n\nRefs: #{FEATURE.number}\nRefs: #2")
    pin_sha = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", f"Spike follow-up\n\nRefs: #{FEATURE.number}\nRefs: #2")
    pin_ref = f"refs/heads/{spiker.branch}"
    git("merge-base", "--is-ancestor", pin_sha, pin_ref)
    pin = h.spike_delivery(spiker, snap.tree, issue(1), pin_ref, SpikeResult(pin_ref, pin_sha, True))
    git("switch", "-q", "master")
    worker = inputs(snap, Role.BUILDER, 5, approval=decide(feature()).approval)
    message = f"Implement fixture\n\nRefs: #{FEATURE.number}\nRefs: #5"
    assert h.valid_work_commit(worker, message)
    for wrong in (message + "\nextra", message.replace("#5", "#6"), message.replace("\n\n", "\n")):
        assert not h.valid_work_commit(worker, wrong)
    worktree = worker.worktree(tmp_path / "worktrees")
    git("worktree", "add", "-q", "-b", worker.branch, str(worktree))
    assert git("branch", "--show-current", cwd=worktree) == worker.branch
    assert git("branch", "--show-current") == "master"
    (worktree / "fixture").write_text("story")
    git("add", "fixture", cwd=worktree)
    git("commit", "-q", "-m", message, cwd=worktree)
    head = git("rev-parse", "HEAD", cwd=worktree)
    assert h.valid_work_commit(worker, git("show", "-s", "--format=%B", head))
    git("merge", "-q", "--squash", worker.branch)
    git("commit", "-q", "-m", message.replace("Implement", "Squash"))
    squash = git("rev-parse", "HEAD")
    assert head != squash and h.valid_work_commit(worker, git("show", "-s", "--format=%B", squash))
    observed = pr(200, 5, squash, approved_revision=worker.approval.sha, head_branch=worker.branch)
    # The fixture GitHub inventory persists PR identity independently of local refs.
    prs = {observed.number: observed}
    trace = h.pr_trace(worker, observed)
    git("worktree", "remove", str(worktree))
    git("branch", "-D", worker.branch)
    assert worker.branch not in git("branch", "--format=%(refname:short)").splitlines()
    resolved = prs[int(trace.pr_url.rsplit("/", 1)[-1])]
    assert h.pr_trace(worker, resolved) == trace
    assert trace.issue == worker.issue and trace.pr_url.endswith("/pull/200")
    assert git("rev-parse", trace.squash_url.rsplit("/", 1)[-1]) == squash
    assert git("rev-parse", pin.commit_url.rsplit("/", 1)[-1]) == pin_sha
    git("merge-base", "--is-ancestor", pin_sha, pin_ref)
    with pytest.raises(h.HandoffError):
        h.pr_trace(worker, replace(observed, squash_verified=False))
