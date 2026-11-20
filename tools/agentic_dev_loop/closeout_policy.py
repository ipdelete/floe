"""Trusted, local pre-push policy. No GitHub writer or native session access."""

import json
import os
import platform
import re
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath

from .closeout import FrozenCloseout, _SUMMARY_LIMIT
from .feature_tree import IssueRef
from .handoffs import _check_id, _feature_tag
from .readiness import valid_sha

_INDEX_LIMIT = 256 * 1024
_FILES = (("index.json", _INDEX_LIMIT), ("summary.md", _SUMMARY_LIMIT))
_BINARY_LIMIT = 32 * 1024 * 1024
# SHA-256 of the extracted Gitleaks v8.30.0 Linux release executables, not the archives.
_BINARY_SHA256 = {
    "aarch64": "3957ad3e168b6633307c1d8ea43bffa17557f825027504b13b574899212e635a",
    "x86_64": "8b6fd684fcd5b4ebe39b68abb072ce59e1063ce7ed4abd556157697845f1f088",
}
_BEARER_RULE = "closeout-bearer"
_RULES = rf"""[extend]
useDefault = true

[[rules]]
id = "{_BEARER_RULE}"
description = "Credential after a Bearer label"
regex = '''(?i)\bbearer[ \t]+([a-z0-9._~+/=-]{{12,}})\b'''
secretGroup = 1
keywords = ["bearer"]
"""
_CONTROL = b"Authorization: Bearer synthetic-control-value-012345\n"
_FINDINGS_EXIT = 23
_SCAN_TIMEOUT = 30
_POSITIVE = r"[1-9][0-9]*"
_REDACTION_PATTERNS = tuple(map(re.compile, (
    r'''(?i)(?:(?<![\w./-])/(?:home|Users|root|tmp|private)/|~[/\\]|\b[A-Z]:\\Users\\|file://)[^\s"'`<>]+''',
    r"\b(?:gh[pousr]_|github_pat_)[a-zA-Z0-9_*….]{3,}",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)\bbearer[ \t]+(?:[a-z0-9._~+/*…=-]{12,}|\*{3,}[a-z0-9]*)",
)))
_LABELLED_SECRET = re.compile(
    r'''(?i)\b(?:api[_ -]?key|password|secret|(?:access[_ -]?)?token)["']?[ \t]*[:=][ \t]*'''
    r'''("[^"\n]*"|'[^'\n]*'|[^\s"'`<>]+)'''
)


class ReleaseStatus(StrEnum):
    RELEASED = "released"
    INVALID_CANDIDATE = "invalid candidate"
    REDACTION_REQUIRED = "redaction required"
    SCANNER_UNVERIFIED = "scanner unverified"
    SCANNER_ERROR = "scanner error"
    FINDING = "scanner finding"
    CANDIDATE_CHANGED = "candidate changed"


@dataclass(frozen=True, slots=True)
class SealedFile:
    path: PurePosixPath
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class SealedCloseout:
    """The only delta the trusted callback may write; no staging paths."""

    files: tuple[SealedFile, ...]


def _record(value: object, *keys: str) -> dict:
    if not isinstance(value, dict) or value.keys() != set(keys):
        raise ValueError
    return value


def _array(value: object) -> list:
    if not isinstance(value, list):
        raise ValueError
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def _decoded_index(data: bytes, feature: IssueRef, tag: str) -> str:
    """Validate the version-1 public schema and return its decoded string leaves."""
    index = _record(json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object),
                    "version", "feature", "design", "stories", "sessions", "omissions")
    if type(index["version"]) is not int or index["version"] != 1:
        raise ValueError
    url = f"https://github.com/{feature.repo}"
    feature_issue = f"{url}/issues/{feature.number}"

    def issue_number(value: object, kind: str) -> str:
        match = re.fullmatch(rf"{re.escape(url)}/{kind}/({_POSITIVE})", value) if isinstance(value, str) else None
        if match is None:
            raise ValueError
        return match[1]

    def check_merge(record: dict) -> None:
        issue_number(record["pr"], "pull")
        prefix = f"{url}/commit/"
        squash = record["squash"]
        if not isinstance(squash, str) or not squash.startswith(prefix) or not valid_sha(squash[len(prefix):]):
            raise ValueError

    if index["feature"] != {"label": tag, "issue": feature_issue}:
        raise ValueError
    design = _record(index["design"], "issue", "pr", "squash")
    issue_number(design["issue"], "issues")
    check_merge(design)
    if design["issue"] == feature_issue:
        raise ValueError

    story_issues = set()
    for value in _array(index["stories"]):
        if isinstance(value, dict) and value.get("status") == "merged":
            story = _record(value, "issue", "status", "pr", "squash")
            check_merge(story)
        else:
            story = _record(value, "issue", "status")
            if story["status"] != "superseded":
                raise ValueError
        issue_number(story["issue"], "issues")
        if story["issue"] in story_issues or story["issue"] in (feature_issue, design["issue"]):
            raise ValueError
        story_issues.add(story["issue"])

    sessions = _array(index["sessions"])
    omissions = _array(index["omissions"])
    ids = set()
    labels = set()
    archivist = False
    label_pattern = re.compile(rf"{re.escape(tag)} — (intake|spike|designer|planner|builder|archivist) "
                               rf"#({_POSITIVE})(?: — attempt (?:[2-9]|[1-9][0-9]+))?")
    for records, omitted in ((sessions, False), (omissions, True)):
        for value in records:
            row = _record(value, "issue", "label", "reason" if omitted else "id")
            number = issue_number(row["issue"], "issues")
            label = row["label"]
            match = label_pattern.fullmatch(label) if isinstance(label, str) else None
            if match is None or match[2] != number or label in labels:
                raise ValueError
            role = match[1]
            if (role in {"intake", "planner", "archivist"}) != (row["issue"] == feature_issue):
                raise ValueError
            if role == "builder" and row["issue"] not in story_issues:
                raise ValueError
            labels.add(label)
            if omitted:
                if row["reason"] != "native-session-missing":
                    raise ValueError
            else:
                _check_id(row["id"])
                if row["id"] in ids:
                    raise ValueError
                ids.add(row["id"])
                archivist |= role == "archivist"
    if not archivist:
        raise ValueError

    records = [index["feature"], design, *index["stories"], *sessions, *omissions]
    return "\n".join(text for record in records for text in record.values()) + "\n"


def _requires_redaction(text: str) -> bool:
    return (any(pattern.search(text) for pattern in _REDACTION_PATTERNS)
            or any(match[1] not in {"[REDACTED]", '"[REDACTED]"', "'[REDACTED]'"}
                   for match in _LABELLED_SECRET.finditer(text)))


@contextmanager
def _open(path: str | Path, flags: int = 0, *, parent: int | None = None) -> Iterator[int]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | flags, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_ctime_ns


def _private(info: os.stat_result) -> bool:
    return info.st_uid == os.geteuid() and not info.st_mode & 0o077


def _layout(root: Path, name: str, expected: tuple[bytes, bytes]) -> tuple[tuple[int, int, int], ...]:
    """Verify the staged copies through no-follow descriptors and record identities."""
    with _open(root, os.O_DIRECTORY) as root_fd, _open(name, os.O_DIRECTORY, parent=root_fd) as directory:
        root_info, stage_info = os.fstat(root_fd), os.fstat(directory)
        if not _private(root_info) or not _private(stage_info):
            raise ValueError
        identities = [_identity(root_info), _identity(stage_info)]
        names = {path for path, _ in _FILES}
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name not in names:
                    raise ValueError
                names.remove(entry.name)
        if names:
            raise ValueError
        for (path, limit), data in zip(_FILES, expected, strict=True):
            with _open(path, parent=directory) as fd:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or not _private(info) or info.st_nlink != 1
                        or info.st_size != len(data)):
                    raise ValueError
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    if stream.read(limit + 1) != data or _identity(info) != _identity(os.fstat(fd)):
                        raise ValueError
                identities.append(_identity(info))
        return tuple(identities)


def _scanner_bytes(binary: Path) -> bytes | None:
    pin = _BINARY_SHA256.get(platform.machine()) if platform.system() == "Linux" else None
    if pin is None or not isinstance(binary, Path) or not binary.is_absolute():
        return None
    try:
        with _open(binary) as fd:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _BINARY_LIMIT:
                return None
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(_BINARY_LIMIT + 1)
    except (OSError, ValueError):
        return None
    return data if sha256(data).hexdigest() == pin else None


def _scan(command: list[str], work: Path, data: bytes) -> list[str]:
    result = subprocess.run(command, input=data, capture_output=True, timeout=_SCAN_TIMEOUT, cwd=work, env={})
    report = json.loads(result.stdout, object_pairs_hook=_unique_object)
    if result.returncode == 0 and report == [] and not result.stderr.strip():
        return []
    if result.returncode != _FINDINGS_EXIT or not isinstance(report, list) or not report:
        raise ValueError
    rules = []
    for finding in report:
        if (not isinstance(finding, dict) or not isinstance(finding.get("RuleID"), str) or not finding["RuleID"]
                or finding.get("Secret") != "REDACTED" or not isinstance(finding.get("Match"), str)):
            raise ValueError
        rules.append(finding["RuleID"])
    return rules


def _scan_views(binary: Path, views: tuple[bytes, ...]) -> ReleaseStatus | None:
    scanner = _scanner_bytes(binary)
    if scanner is None:
        return ReleaseStatus.SCANNER_UNVERIFIED
    try:
        with tempfile.TemporaryDirectory(prefix="floe-closeout-scan-", dir="/tmp") as temp:
            work = Path(temp)
            exe, config = work / "gitleaks", work / "rules.toml"
            # Execute only a private copy of the exact bytes that passed the pin.
            exe.write_bytes(scanner)
            exe.chmod(0o500)
            config.write_text(_RULES, encoding="utf-8")
            command = [str(exe), "stdin", "--config", str(config), "--no-banner", "--no-color",
                       "--redact=100", "--ignore-gitleaks-allow", "--log-level", "warn", "--exit-code",
                       str(_FINDINGS_EXIT), "--report-format", "json", "--report-path", "-"]
            if _scan(command, work, _CONTROL) != [_BEARER_RULE]:
                return ReleaseStatus.SCANNER_ERROR
            for data in views:
                if _scan(command, work, data):
                    return ReleaseStatus.FINDING
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        # Never render the exception, stdout, stderr or a scanner report.
        return ReleaseStatus.SCANNER_ERROR
    return None


def release_closeout(candidate: FrozenCloseout, staging_root: Path, feature: IssueRef, slug: str,
                     scanner: Path, publish: Callable[[SealedCloseout], None]) -> ReleaseStatus:
    """Gate one private handoff; invoke the trusted writer only with scanned bytes.

    The operator supplies protected policy, root (including its parents),
    scanner and writer. The writer must use only the immutable delta, never
    reread a staging tree, and handle its own authorization and write recovery.
    No result from this function is evidence of GitHub state.
    """
    try:
        tag = _feature_tag(feature, slug)
        if (type(candidate) is not FrozenCloseout or not isinstance(staging_root, Path)
                or not staging_root.is_absolute() or not isinstance(candidate.directory, Path)
                or candidate.directory.parent != staging_root or not callable(publish)):
            return ReleaseStatus.INVALID_CANDIDATE
        name = candidate.directory.name
        if not re.fullmatch(rf"{re.escape(tag)}-[a-z0-9_]{{8}}", name):
            return ReleaseStatus.INVALID_CANDIDATE
        data = (candidate.index, candidate.summary)
        if any(type(blob) is not bytes or not 0 < len(blob) <= limit
               for (_, limit), blob in zip(_FILES, data, strict=True)):
            return ReleaseStatus.INVALID_CANDIDATE
        before_scan = _layout(staging_root, name, data)
        decoded = _decoded_index(data[0], feature, tag)
        summary = data[1].decode("utf-8")
        if not summary.strip() or any(unicodedata.category(char) in {"Cc", "Cf"} and char not in "\n\r\t"
                                      for char in summary):
            return ReleaseStatus.INVALID_CANDIDATE
    except (OSError, ValueError, RecursionError):  # Includes Unicode and handoff schema errors.
        return ReleaseStatus.INVALID_CANDIDATE

    if _requires_redaction(decoded) or _requires_redaction(summary):
        return ReleaseStatus.REDACTION_REQUIRED
    status = _scan_views(scanner, (data[0], decoded.encode("utf-8"), data[1]))
    if status is not None:
        return status
    try:
        if _layout(staging_root, name, data) != before_scan:
            return ReleaseStatus.CANDIDATE_CHANGED
    except (OSError, ValueError):
        return ReleaseStatus.CANDIDATE_CHANGED

    prefix = PurePosixPath(".agents", "engineering", tag)
    sealed = SealedCloseout(tuple(SealedFile(prefix / path, blob)
                                  for (path, _), blob in zip(_FILES, data, strict=True)))
    publish(sealed)
    return ReleaseStatus.RELEASED
