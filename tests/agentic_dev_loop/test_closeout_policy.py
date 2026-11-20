import json
import os
import platform
import subprocess
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from test_closeout import ARCHIVIST_ID, SLUG, SUMMARY, TAG, URL, freeze, inputs  # noqa: F401
from test_readiness import FEATURE
from tools.agentic_dev_loop import closeout_policy


@pytest.fixture
def gate():
    return closeout_policy


def report(rule_id, match="REDACTED"):
    return json.dumps([{"RuleID": rule_id, "Secret": "REDACTED", "Match": match}]).encode()


def run_gate(gate, frozen, root, scanner, writes):
    return gate.release_closeout(frozen, root, FEATURE, SLUG, scanner, writes.append)


def change(frozen, name, data):
    path = frozen.directory / name
    path.chmod(0o600)
    path.write_bytes(data)
    path.chmod(0o400)
    return replace(frozen, **{"index" if name == "index.json" else "summary": data})


@pytest.fixture
def candidate(inputs):
    frozen = freeze(inputs)
    frozen.directory.chmod(0o700)  # exercise private edits even by a non-root owner
    return frozen


@pytest.fixture
def fake_scanner(tmp_path, monkeypatch, gate):
    binary = tmp_path / "scanner"
    binary.write_bytes(b"synthetic pinned binary")
    # Patch only the platform pin and subprocess boundary, not the policy or filesystem.
    monkeypatch.setitem(gate._BINARY_SHA256, platform.machine(), sha256(binary.read_bytes()).hexdigest())
    calls = []
    control_report = report("closeout-bearer")

    def clean(args, **kwargs):
        calls.append(kwargs["input"])
        executable = Path(args[0])
        assert executable != binary and executable.read_bytes() == binary.read_bytes()
        assert executable.parent == kwargs["cwd"]
        assert kwargs["env"] == {} and kwargs["timeout"] > 0 and kwargs["capture_output"]
        assert "--ignore-gitleaks-allow" in args and "--redact=100" in args
        assert 'id = "closeout-bearer"' in Path(args[args.index("--config") + 1]).read_text()
        if len(calls) == 1:
            assert b"Bearer " in calls[0]
            return subprocess.CompletedProcess(args, 23, control_report, b"warning: control")
        return subprocess.CompletedProcess(args, 0, b"[]\n", b"")

    monkeypatch.setattr(subprocess, "run", clean)
    return binary, calls, clean


def test_clean_freezes_exact_public_paths_and_scans_both_index_views(gate, candidate, inputs, fake_scanner):
    binary, calls, _ = fake_scanner
    writes = []
    assert run_gate(gate, candidate, inputs[0], binary, writes) is gate.ReleaseStatus.RELEASED
    assert len(writes) == 1
    assert [(str(file.path), file.data) for file in writes[0].files] == [
        (f".agents/engineering/{TAG}/index.json", candidate.index),
        (f".agents/engineering/{TAG}/summary.md", SUMMARY.encode()),
    ]
    _, raw_index, decoded, raw_summary = calls
    assert (raw_index, raw_summary) == (candidate.index, candidate.summary)
    assert decoded != raw_index
    assert f"{TAG} — archivist #{FEATURE.number}".encode() in decoded.splitlines()
    assert f"{URL}/issues/{FEATURE.number}".encode() in decoded.splitlines()
    assert ARCHIVIST_ID.encode() in decoded.splitlines()


@pytest.mark.parametrize("problem", ["json", "duplicate", "version", "key", "label", "link", "no-archivist",
                                     "index-size", "summary-size", "summary-utf8", "summary-control", "home",
                                     "masked-token"])
def test_invalid_or_unredacted_content_is_held_without_scanning(gate, candidate, inputs, fake_scanner, problem):
    binary, calls, _ = fake_scanner
    if problem.startswith("summary-") or problem in {"home", "masked-token"}:
        data = {"summary-size": b"a" * 65537, "summary-utf8": b"\xff", "summary-control": b"body\x00",
                "home": b"Private location: /home/synthetic/private.txt\n",
                "masked-token": b"An old token fragment: ghp_****\n"}[problem]
        candidate = change(candidate, "summary.md", data)
    else:
        document = json.loads(candidate.index)
        if problem == "version":
            document["version"] = True
        elif problem == "key":
            document["sessions"][0]["path"] = "transcripts/builder.jsonl"
        elif problem == "label":
            document["sessions"][0]["label"] += " copied metadata"
        elif problem == "link":
            document["design"]["pr"] += "/../../transcripts/native.jsonl"
        elif problem == "no-archivist":
            document["sessions"].pop()
        data = {"json": b"[broken", "duplicate": b'{"version":0,' + candidate.index.lstrip()[1:],
                "index-size": b" " * (2 * 1024 * 1024 + 1)}.get(problem, json.dumps(document).encode())
        candidate = change(candidate, "index.json", data)
    writes = []
    status = run_gate(gate, candidate, inputs[0], binary, writes)
    assert status is (gate.ReleaseStatus.REDACTION_REQUIRED if problem in {"home", "masked-token"}
                      else gate.ReleaseStatus.INVALID_CANDIDATE)
    assert not calls and not writes
    assert candidate.directory.is_dir()


@pytest.mark.parametrize("problem", ["misplaced", "renamed", "extra", "transcript", "file-symlink", "dir-symlink", "fifo"])
def test_wrong_layout_is_held_without_scanning(gate, candidate, inputs, tmp_path, fake_scanner, problem):
    binary, calls, _ = fake_scanner
    d = candidate.directory
    if problem == "misplaced":
        candidate = replace(candidate, directory=d.rename(tmp_path / d.name))
    elif problem == "renamed":
        (d / "summary.md").rename(d / "renamed.md")
    elif problem in {"extra", "transcript"}:
        (d / ("extra.md" if problem == "extra" else "transcripts")).mkdir()
    elif problem == "dir-symlink":
        target = tmp_path / d.name
        d.rename(target)
        d.symlink_to(target, target_is_directory=True)
    else:
        summary = d / "summary.md"
        summary.unlink()
        if problem == "fifo":
            os.mkfifo(summary)
        else:
            target = tmp_path / "other.md"
            target.write_bytes(candidate.summary)
            summary.symlink_to(target)
    writes = []
    assert run_gate(gate, candidate, inputs[0], binary, writes) is gate.ReleaseStatus.INVALID_CANDIDATE
    assert not calls and not writes


@pytest.mark.parametrize("problem", ["checksum", "missing", "spawn", "timeout", "exit", "malformed", "warning",
                                     "control-miss", "wrong-control-rule", "malformed-control", "decoded-finding"])
def test_scanner_failures_do_not_release_or_report_match_text(gate, candidate, inputs, fake_scanner,
                                                          monkeypatch, capsys, problem):
    binary, calls, clean = fake_scanner
    diagnostic = b"private fixture match text"
    if problem == "checksum":
        binary.write_bytes(b"wrong binary")
    elif problem == "missing":
        binary.unlink()

    def fail(args, **kwargs):
        if problem in {"checksum", "missing"}:
            pytest.fail("attempted to execute an unverified binary")
        if problem == "control-miss":
            return subprocess.CompletedProcess(args, 0, b"[]", b"")
        if problem == "wrong-control-rule":
            return subprocess.CompletedProcess(args, 23, report("other"), diagnostic)
        if problem == "malformed-control":
            return subprocess.CompletedProcess(args, 23, b'[{"RuleID":"closeout-bearer"}]', diagnostic)
        completed = clean(args, **kwargs)
        if len(calls) < 3:  # control and exact index succeed; target the decoded index
            return completed
        if problem == "timeout":
            raise subprocess.TimeoutExpired(args, 30, output=diagnostic)
        if problem == "spawn":
            raise OSError(diagnostic.decode())
        code, stdout, stderr = {
            "exit": (1, b"[]", diagnostic), "malformed": (0, b"not json: " + diagnostic, b""),
            "warning": (0, b"[]", diagnostic),
            "decoded-finding": (23, report("fixture", diagnostic.decode()), diagnostic),
        }[problem]
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", fail)
    writes = []
    status = run_gate(gate, candidate, inputs[0], binary, writes)
    expected = {"checksum": gate.ReleaseStatus.SCANNER_UNVERIFIED, "missing": gate.ReleaseStatus.SCANNER_UNVERIFIED,
                "decoded-finding": gate.ReleaseStatus.FINDING}
    assert status is expected.get(problem, gate.ReleaseStatus.SCANNER_ERROR)
    assert not writes and candidate.directory.is_dir()
    assert capsys.readouterr() == ("", "")
    assert diagnostic.decode() not in str(status)


@pytest.mark.parametrize("swap", ["bytes", "path", "same-bytes-new-file", "extra"])
def test_replacement_at_end_of_passing_scan_cannot_reach_callback(gate, candidate, inputs, fake_scanner,
                                                               monkeypatch, swap):
    binary, calls, clean = fake_scanner
    writes = []

    def replace_after_scan(args, **kwargs):
        completed = clean(args, **kwargs)
        if kwargs["input"] == candidate.summary:
            if swap == "bytes":
                change(candidate, "summary.md", b"Corrected privately.\n")
            elif swap == "path":
                (candidate.directory / "summary.md").rename(candidate.directory / "renamed.md")
            elif swap == "same-bytes-new-file":
                (candidate.directory / "summary.md").rename(candidate.directory.parent / "old-summary")
                (candidate.directory / "summary.md").write_bytes(candidate.summary)
                (candidate.directory / "summary.md").chmod(0o400)
            else:
                (candidate.directory / "transcript.jsonl").touch()
        return completed

    monkeypatch.setattr(subprocess, "run", replace_after_scan)
    assert run_gate(gate, candidate, inputs[0], binary, writes) is gate.ReleaseStatus.CANDIDATE_CHANGED
    assert calls[-1] == candidate.summary and not writes
    if swap == "bytes":
        corrected = change(candidate, "summary.md", b"Corrected privately.\n")
        calls.clear()
        monkeypatch.setattr(subprocess, "run", clean)
        assert run_gate(gate, corrected, inputs[0], binary, writes) is gate.ReleaseStatus.RELEASED
        assert calls[-1] == writes[0].files[-1].data == corrected.summary


def test_callback_cannot_see_a_late_staging_swap_in_its_delta(gate, candidate, inputs, fake_scanner):
    binary, _, _ = fake_scanner
    writes = []

    def fake_publish(sealed):
        change(candidate, "summary.md", b"Late replacement.\n")
        writes.extend(sealed.files)

    assert gate.release_closeout(candidate, inputs[0], FEATURE, SLUG, binary, fake_publish) is gate.ReleaseStatus.RELEASED
    assert writes[-1].data == candidate.summary
    assert writes[-1].data != (candidate.directory / "summary.md").read_bytes()


def test_pinned_gitleaks_on_synthetic_inputs(gate, candidate, inputs, capsys):
    scanner = os.environ.get("FLOE_TEST_GITLEAKS")
    if not scanner:
        pytest.skip("set FLOE_TEST_GITLEAKS to the pinned release binary for the offline integration check")
    binary = Path(scanner)
    writes = []
    # The control itself is a finding; it must never be treated as a clean scan.
    assert gate._scan_views(binary, (gate._CONTROL,)) is gate.ReleaseStatus.FINDING
    assert run_gate(gate, candidate, inputs[0], binary, writes) is gate.ReleaseStatus.RELEASED
    assert len(writes) == 1
    writes.clear()
    # Built-in scanner rule, independent of the residual redaction patterns.
    seed = "npm_" + "7Z2k9TyHF4g0QWcMfb3EVNnPau1Jd8zm2XXq"
    dirty = change(candidate, "summary.md", f"{seed} <!-- gitleaks:allow -->\n".encode())
    assert run_gate(gate, dirty, inputs[0], binary, writes) is gate.ReleaseStatus.FINDING
    assert not writes and capsys.readouterr() == ("", "")
