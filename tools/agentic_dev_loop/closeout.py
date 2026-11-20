"""Private closeout staging for a trusted fixture supervisor, not publication."""

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .feature_tree import IssueRef, Phase
from .handoffs import HandoffError, _check_id, _feature_tag
from .readiness import Decision, FeatureEvidence, Policy, Reason, Role, decide_feature, valid_sha

_HEADER_LIMIT = 16 * 1024
_SUMMARY_LIMIT = 64 * 1024
_TARGET_PHASES = {Role.SPIKE: Phase.SPIKE, Role.DESIGNER: Phase.DESIGN, Role.BUILDER: Phase.STORY}
_FEATURE_ROLES = ("intake", Role.PLANNER, Role.ARCHIVIST)


class CloseoutBlocked(ValueError):
    """The candidate could not be staged. The message contains no private data."""


@dataclass(frozen=True, slots=True)
class CloseoutDraft:
    feature: IssueRef
    slug: str
    approved_revision: str
    archivist_id: str | None
    summary: str = field(repr=False)  # independently authored, never extracted from a session


@dataclass(frozen=True, slots=True)
class Session:
    """Trusted binding to a finished (or not yet finished) native Pi session."""

    role: str
    issue: int
    attempt: int
    session_id: str
    native_path: Path = field(repr=False)
    finished: bool
    allow_missing: bool = False


@dataclass(frozen=True, slots=True)
class FrozenCloseout:
    """Exact candidate bytes for a later policy gate; not cleared for release."""

    directory: Path = field(repr=False)
    index: bytes = field(repr=False)
    summary: bytes = field(repr=False)


def _summary_bytes(summary: object) -> bytes:
    if not isinstance(summary, str) or not summary.strip() or len(summary) > _SUMMARY_LIMIT:
        raise CloseoutBlocked("invalid closeout summary")
    try:
        data = summary.encode("utf-8")
    except UnicodeError:
        raise CloseoutBlocked("invalid closeout summary") from None
    if len(data) > _SUMMARY_LIMIT:
        raise CloseoutBlocked("invalid closeout summary")
    return data


def _native_present(session: Session) -> bool:
    # The supervisor controls the parent directories. Reject even a dangling
    # final symlink and never block opening a FIFO in place of a regular file.
    try:
        fd = os.open(session.native_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        raise CloseoutBlocked("native session could not be inspected") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CloseoutBlocked("invalid native session header")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            header = stream.readline(_HEADER_LIMIT + 1)
    except OSError:
        raise CloseoutBlocked("native session could not be inspected") from None
    finally:
        os.close(fd)
    if len(header) > _HEADER_LIMIT or not header.endswith(b"\n"):
        raise CloseoutBlocked("invalid native session header")
    try:
        entry = json.loads(header)
    except (ValueError, RecursionError):
        raise CloseoutBlocked("invalid native session header") from None
    if (not isinstance(entry, dict) or entry.get("type") != "session"
            or entry.get("id") != session.session_id):
        raise CloseoutBlocked("invalid native session header")
    return True


def _session_index(sessions: tuple[Session, ...], draft: CloseoutDraft, phases: Mapping[int, Phase],
                   superseded: set[int], tag: str, url: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not isinstance(sessions, tuple):
        raise CloseoutBlocked("complete session inventory required")
    included = []
    omitted = []
    ids = set()
    attempts = set()
    archivist_found = False
    for session in sessions:
        if (not isinstance(session, Session) or not isinstance(session.role, str)
                or type(session.issue) is not int or session.issue <= 0
                or type(session.attempt) is not int or session.attempt <= 0
                or not isinstance(session.native_path, Path) or not session.native_path.is_absolute()
                or type(session.allow_missing) is not bool):
            raise CloseoutBlocked("invalid session binding")
        try:
            _check_id(session.session_id)
        except HandoffError:
            raise CloseoutBlocked("invalid session identifier") from None
        if session.role in _FEATURE_ROLES:
            linked = session.issue == draft.feature.number
        elif session.role == Role.BUILDER and session.issue in superseded:
            linked = True  # a superseded story may no longer be linked
        else:
            phase = _TARGET_PHASES.get(session.role)
            linked = phase is not None and phases.get(session.issue) is phase
        attempt = (session.role, session.issue, session.attempt)
        if not linked or attempt in attempts or session.session_id in ids:
            raise CloseoutBlocked("invalid or duplicate session binding")
        if session.finished is not True:
            raise CloseoutBlocked("listed session has not finished")
        attempts.add(attempt)
        ids.add(session.session_id)
        is_archivist = session.session_id == draft.archivist_id
        if is_archivist and session.role != Role.ARCHIVIST:
            raise CloseoutBlocked("draft session is not an Archivist session")
        suffix = "" if session.attempt == 1 else f" — attempt {session.attempt}"
        label = f"{tag} — {session.role} #{session.issue}{suffix}"
        link = f"{url}/issues/{session.issue}"
        if _native_present(session):
            included.append({"id": session.session_id, "label": label, "issue": link})
            archivist_found |= is_archivist
        elif session.allow_missing and not is_archivist:
            omitted.append({"label": label, "issue": link, "reason": "native-session-missing"})
        else:
            raise CloseoutBlocked("required native session missing")
    if not archivist_found:
        raise CloseoutBlocked("Archivist session is not in the index")
    return included, omitted


def _write_candidate(root: Path, tag: str, index: bytes, summary: bytes) -> FrozenCloseout:
    directory = None
    try:
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise CloseoutBlocked("staging root must be a private owned directory")
        directory = Path(tempfile.mkdtemp(dir=root, prefix=f"{tag}-"))
        for name, data in (("index.json", index), ("summary.md", summary)):
            path = directory / name
            with path.open("xb") as stream:
                stream.write(data)
            path.chmod(0o400)
        directory.chmod(0o500)
    except OSError:
        if directory is not None:
            try:
                directory.chmod(0o700)
            except OSError:
                pass  # Still try removing the failed stage.
            try:
                shutil.rmtree(directory)
            except OSError:
                raise CloseoutBlocked("private staging failed and cleanup is unverified") from None
        raise CloseoutBlocked("private staging failed; no candidate handed off") from None
    return FrozenCloseout(directory, index, summary)


def public_scope(feature: IssueRef, decision: Decision) -> dict:
    """The version-1 index projection of a completed backlog and its approval."""
    approval = decision.approval
    if not decision.closeout_ready or approval is None:
        raise CloseoutBlocked("approved backlog is not ready")
    url = f"https://github.com/{feature.repo}"
    return {
        "design": {"issue": f"{url}/issues/{approval.issue}", "pr": f"{url}/pull/{approval.pr}",
                   "squash": f"{url}/commit/{approval.sha}"},
        "stories": [
            {"issue": f"{url}/issues/{story.issue}", "status": "merged", "pr": f"{url}/pull/{story.pr}",
             "squash": f"{url}/commit/{story.squash_sha}"}
            for story in decision.completed_stories
        ] + [{"issue": f"{url}/issues/{number}", "status": "superseded"}
             for number in sorted(b.issue for b in decision.blockers if b.reason is Reason.SUPERSEDED)],
    }


def stage_closeout(root: Path, policy: Policy, evidence: FeatureEvidence, draft: CloseoutDraft,
                   sessions: tuple[Session, ...]) -> FrozenCloseout:
    """Run after Archivist exit with fresh normalized evidence and a complete roster.

    The caller owns evidence provenance, trusted storage and session completion
    checks. No process, network operation or model call is made here.
    """
    if (not isinstance(draft, CloseoutDraft) or not isinstance(root, Path)
            or not isinstance(policy, Policy) or not isinstance(evidence, FeatureEvidence)):
        raise CloseoutBlocked("invalid staging inputs")
    try:
        tag = _feature_tag(draft.feature, draft.slug)
        _check_id(draft.archivist_id)
    except HandoffError:
        raise CloseoutBlocked("invalid feature or Archivist binding") from None
    if not valid_sha(draft.approved_revision):
        raise CloseoutBlocked("invalid draft revision")
    decision = decide_feature(draft.feature, policy, evidence)
    approval = decision.approval
    if not decision.closeout_ready or approval is None or approval.sha != draft.approved_revision:
        raise CloseoutBlocked("approved backlog is not ready for this closeout draft")
    superseded = {blocker.issue for blocker in decision.blockers if blocker.reason is Reason.SUPERSEDED}
    url = f"https://github.com/{draft.feature.repo}"
    summary = _summary_bytes(draft.summary)
    included, omitted = _session_index(sessions, draft, evidence.tree.phases, superseded, tag, url)
    index = {
        "version": 1,
        "feature": {"label": tag, "issue": f"{url}/issues/{draft.feature.number}"},
        **public_scope(draft.feature, decision),
        "sessions": included,
        "omissions": omitted,
    }
    data = (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return _write_candidate(root, tag, data, summary)
