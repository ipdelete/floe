import json
import subprocess
from dataclasses import replace
from stat import S_IMODE

import pytest

from test_readiness import FEATURE, FIRST, OPEN, POLICY, SECOND, completed, design, feature, story
from tools.agentic_dev_loop.closeout import CloseoutBlocked, CloseoutDraft, Session, stage_closeout
from tools.agentic_dev_loop.feature_tree import IssueRef
from tools.agentic_dev_loop.readiness import Backlog, DesignEvidence, RevisionRequest

SLUG = "fixture-feature"
TAG = f"{FEATURE.number}-{SLUG}"
URL = f"https://github.com/{FEATURE.repo}"
BUILDER_ID = "00000000-0000-0000-0000-000000000001"
ARCHIVIST_ID = "00000000-0000-0000-0000-000000000002"
SUMMARY = "The work is complete. This overview was written for closeout.\n"


def native(path, session_id):
    path.write_text("\n".join(json.dumps(record) for record in (
        {"type": "session", "id": session_id, "cwd": "/private/worktree"},
        {"type": "session_info", "name": "Private label to discard"},
        {"type": "message", "message": {"role": "user", "content": "Synthetic dialogue"}},
        {"type": "message", "message": {"role": "toolResult", "content": "Synthetic tool result"}},
    )) + "\n")
    # The body isn't evidence of identity or completion and must not be parsed.
    with path.open("ab") as stream:
        stream.write(b"\xff invalid synthetic tail\n")
    return path


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "staging"
    root.mkdir(mode=0o700)
    builder = Session("builder", 5, 1, BUILDER_ID, native(tmp_path / "private-builder.jsonl", BUILDER_ID), True)
    archivist = Session("archivist", FEATURE.number, 1, ARCHIVIST_ID,
                        native(tmp_path / "private-archivist.jsonl", ARCHIVIST_ID), True)
    snapshot = feature(
        designs={1: design(), 2: design(2, SECOND, 1)},
        stories={5: completed(5, revision=SECOND), 6: story()},
        backlogs=(Backlog(FIRST, (5, 6)), Backlog(SECOND, (5,), (6,))),
        closeout_active=True,  # admission isn't available again while closeout is in progress
    )
    draft = CloseoutDraft(FEATURE, SLUG, SECOND, ARCHIVIST_ID, SUMMARY)
    return root, snapshot, draft, (builder, archivist)


def freeze(inputs):
    root, snapshot, draft, sessions = inputs
    return stage_closeout(root, POLICY, snapshot, draft, sessions)


def test_stage_complete_backlog_with_verified_squash_and_explicit_supersession(inputs):
    root, _, _, _ = inputs
    frozen = freeze(inputs)
    assert frozen.directory.parent == root
    assert frozen.directory.name.startswith(f"{TAG}-")
    assert S_IMODE(frozen.directory.stat().st_mode) == 0o500
    assert {file.name: S_IMODE(file.stat().st_mode) for file in frozen.directory.iterdir()} == {
        "index.json": 0o400, "summary.md": 0o400,
    }
    assert (frozen.directory / "index.json").read_bytes() == frozen.index
    assert (frozen.directory / "summary.md").read_bytes() == frozen.summary == SUMMARY.encode()
    story_sha = completed(5, revision=SECOND).candidates[0].squash_sha
    assert json.loads(frozen.index) == {
        "version": 1,
        "feature": {"label": TAG, "issue": f"{URL}/issues/{FEATURE.number}"},
        "design": {"issue": f"{URL}/issues/2", "pr": f"{URL}/pull/22", "squash": f"{URL}/commit/{SECOND}"},
        "stories": [
            {"issue": f"{URL}/issues/5", "status": "merged", "pr": f"{URL}/pull/35",
             "squash": f"{URL}/commit/{story_sha}"},
            {"issue": f"{URL}/issues/6", "status": "superseded"},
        ],
        "sessions": [
            {"id": BUILDER_ID, "label": f"{TAG} — builder #5", "issue": f"{URL}/issues/5"},
            {"id": ARCHIVIST_ID, "label": f"{TAG} — archivist #{FEATURE.number}",
             "issue": f"{URL}/issues/{FEATURE.number}"},
        ],
        "omissions": [],
    }


@pytest.mark.parametrize("problem", ["open", "wrong-squash", "revision-request", "pending-design", "old-draft"])
def test_backlog_or_revision_discrepancy_writes_nothing(inputs, problem):
    root, snapshot, draft, sessions = inputs
    children = dict(snapshot.children)
    if problem == "open":
        children[5] = replace(children[5], state=OPEN)
    elif problem == "wrong-squash":
        [pr] = children[5].candidates
        children[5] = replace(children[5], candidates=(replace(pr, squash_sha=FIRST, squash_verified=False),))
    elif problem == "revision-request":
        snapshot = replace(snapshot, revision_requests=(RevisionRequest(FEATURE, IssueRef(FEATURE.repo, 5), SECOND, 1),))
    elif problem == "pending-design":
        children[2] = DesignEvidence(OPEN, False, 1, (), None)
    else:
        draft = replace(draft, approved_revision=FIRST)
    with pytest.raises(CloseoutBlocked):
        freeze((root, replace(snapshot, children=children), draft, sessions))
    assert not list(root.iterdir())


def test_missing_native_session_requires_explicit_omission_and_is_not_fabricated(inputs):
    root, snapshot, draft, (builder, archivist) = inputs
    builder.native_path.unlink()
    with pytest.raises(CloseoutBlocked):
        freeze(inputs)
    assert not list(root.iterdir())
    frozen = freeze((root, snapshot, draft, (replace(builder, allow_missing=True), archivist)))
    index = json.loads(frozen.index)
    assert [entry["id"] for entry in index["sessions"]] == [ARCHIVIST_ID]
    assert index["omissions"] == [
        {"label": f"{TAG} — builder #5", "issue": f"{URL}/issues/5", "reason": "native-session-missing"},
    ]
    assert BUILDER_ID.encode() not in frozen.index


@pytest.mark.parametrize("bad_native", ["wrong-id", "malformed", "symlink"])
def test_invalid_native_session_is_not_known_absent(inputs, bad_native):
    root, snapshot, draft, (builder, archivist) = inputs
    if bad_native == "wrong-id":
        native(builder.native_path, ARCHIVIST_ID)
    elif bad_native == "malformed":
        builder.native_path.write_text("not a session\n")
    else:
        builder.native_path.unlink()
        builder.native_path.symlink_to(builder.native_path.with_name("missing-target"))
    with pytest.raises(CloseoutBlocked) as exc:
        freeze((root, snapshot, draft, (replace(builder, allow_missing=True), archivist)))
    assert str(builder.native_path) not in str(exc.value)
    assert not list(root.iterdir())


def test_handoff_only_after_all_listed_sessions_finish_without_launch_or_push(inputs, monkeypatch):
    def no_process(*args, **kwargs):
        pytest.fail("staging invoked an external command")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    root, snapshot, draft, (builder, archivist) = inputs
    for candidate, sessions in (
        (replace(draft, archivist_id=None), (builder, archivist)),
        (draft, (builder,)),
        (draft, (builder, replace(archivist, finished=False))),
        (draft, (replace(builder, finished=False), archivist)),
    ):
        with pytest.raises(CloseoutBlocked):
            freeze((root, snapshot, candidate, sessions))
        assert not list(root.iterdir())
    frozen = freeze(inputs)
    assert [file.name for file in root.iterdir()] == [frozen.directory.name]
    original = frozen.index
    # Handoff owns exact bytes, independent of subsequent changes to native storage.
    archivist.native_path.unlink()
    with pytest.raises(CloseoutBlocked):
        freeze((root, snapshot, draft, (builder, replace(archivist, allow_missing=True))))
    assert frozen.index == original == (frozen.directory / "index.json").read_bytes()


def test_unsafe_staging_root_is_refused(inputs):
    root, _, _, _ = inputs
    root.chmod(0o755)
    with pytest.raises(CloseoutBlocked):
        freeze(inputs)
    assert not list(root.iterdir())
