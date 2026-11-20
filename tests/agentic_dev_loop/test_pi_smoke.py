"""Synthetic credential and native-session evidence; no default live calls."""

import json
import shutil
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import pytest

from tools.agentic_dev_loop import pi_probe as probe
from tools.agentic_dev_loop.installed_fixture import Fixture, REQUEST
from tools.agentic_dev_loop.launcher import AttemptNotFound, AttemptState, Reservation, WorkerUnavailable
from tools.agentic_dev_loop.pi_smoke import SmokeManager, SmokeInputs, live_inputs, provision, read_outcome


def test_gh_receives_token_only_in_child_environment(monkeypatch):
    def run(argv, **kwargs):
        assert "synthetic-token" not in argv
        assert argv[argv.index("--method") + 1] == "GET"
        assert kwargs["env"]["GH_TOKEN"] == "synthetic-token"
        assert kwargs["stdin"] == kwargs["stderr"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 0, stdout=b"HTTP/2.0 200 OK\r\n\r\nreader\n")

    monkeypatch.setattr(probe.subprocess, "run", run)
    assert probe.github("user", "synthetic-token") == (200, "reader")


def test_gh_failure_keeps_http_status_but_not_error_body(monkeypatch):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 1, stdout=b"HTTP/2.0 401 Unauthorized\r\n\r\nuntrusted diagnostic"))
    assert probe.github("user", "synthetic-token") == (401, "")


def test_read_probe_only_performs_fixed_gets(monkeypatch):
    seen = []

    def gh(endpoint, token):
        seen.append((endpoint, token))
        if token == probe.INVALID:
            return 401, ""
        return 200, {"user": "reader", "repos/ipdelete/floe/issues/29": "29"}[endpoint]

    monkeypatch.setattr(probe, "github", gh)
    probe.check_github("tick", "synthetic", "reader")
    assert seen == [("user", probe.INVALID), ("user", "synthetic"),
                    ("repos/ipdelete/floe/issues/29", "synthetic")]


@pytest.mark.parametrize("reply", [(401, "fake secret"), (200, "wrong-user"), (None, "fake secret")])
def test_wrong_or_unknown_worker_auth_has_safe_error(monkeypatch, reply):
    monkeypatch.setattr(probe, "github", lambda endpoint, token:
                        (401, "") if token == probe.INVALID else reply)
    with pytest.raises(probe.Blocked, match="^GitHub identity not verified$"):
        probe.check_github("worker", "synthetic", "expected")


def test_worker_runs_fixed_persistent_command_once(monkeypatch, tmp_path):
    session_id = str(uuid4())
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    monkeypatch.setattr(probe, "SESSIONS", sessions)
    monkeypatch.setattr(probe, "AGENT", tmp_path / "agent")
    auth = tmp_path / "auth.json"
    auth.write_text('{"github-copilot": {"type": "oauth", "access": "fake"}}')
    monkeypatch.setattr(probe, "COPILOT", auth)
    monkeypatch.setattr(probe, "MODELS", tmp_path / "absent-model-cache")
    commands = []

    def fake_run(argv, **kwargs):
        commands.append(argv)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert not {"GH_TOKEN", "SSH_AUTH_SOCK"} & kwargs["env"].keys()
        native(sessions, session_id)
        kwargs["stdout"].write(probe.REPLY.encode())
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    probe.run_pi(session_id)
    [argv] = commands
    assert argv[:3] == ["/node", "/pi/node_modules/.bin/pi", "--print"]
    for flag, value in [("--model", "github-copilot/gpt-6.2-sol"), ("--session-id", session_id),
                        ("--name", probe.SESSION_NAME), ("--session-dir", str(sessions))]:
        assert argv[argv.index(flag) + 1] == value
    assert "--no-session" not in argv and "--no-tools" in argv
    assert argv[-1] == probe.PROMPT
    settings = json.loads((probe.AGENT / "settings.json").read_text())
    assert settings["retry"]["enabled"] is False


def native(directory, session_id, *, embedded=None):
    path = directory / f"2026-01-01T10-00-00_{session_id}.jsonl"
    path.write_text(json.dumps({"type": "session", "id": embedded or session_id, "cwd": "/work"}) + "\n"
                    + json.dumps({"type": "session_info", "name": probe.SESSION_NAME}) + "\n")
    path.chmod(0o600)
    return path


def test_session_must_match_private_native_file(tmp_path):
    tmp_path.chmod(0o700)
    reserved = str(uuid4())
    path = native(tmp_path, reserved, embedded=str(uuid4()))
    assert not probe.verify_session(tmp_path, reserved)
    path.unlink()
    path = native(tmp_path, reserved)
    assert probe.verify_session(tmp_path, reserved)
    path.chmod(0o644)
    assert not probe.verify_session(tmp_path, reserved)


def test_isolation_failure_prevents_even_auth(monkeypatch):
    monkeypatch.setattr(probe, "require_isolation", lambda role: (_ for _ in ()).throw(
        probe.Blocked("required isolation unavailable")))
    monkeypatch.setattr(probe, "check_github", lambda *_: pytest.fail("must not check auth"))
    monkeypatch.setattr(probe, "run_pi", lambda *_: pytest.fail("must not run model"))
    with pytest.raises(probe.Blocked, match="required isolation unavailable"):
        probe.execute("worker", "maintainer", str(uuid4()))


def test_isolation_rejects_working_unix_sockets(monkeypatch):
    monkeypatch.setattr(probe, "check_layout", lambda role: None)
    monkeypatch.setattr(probe.socket, "socket", lambda *args: nullcontext())
    with pytest.raises(probe.Blocked, match="required isolation unavailable"):
        probe.require_isolation("worker")


def test_role_mounts_do_not_share_credentials(tmp_path):
    inputs = SmokeInputs(tmp_path, "reader", "maintainer", tmp_path / "node", tmp_path / "release")
    tick = inputs.sandbox("tick", tmp_path / "tick-io")
    with pytest.raises(ValueError, match="requires an isolated worktree"):
        inputs.sandbox("worker", tmp_path / "worker-io")
    worker = inputs.sandbox("worker", tmp_path / "worker-io", worktree=tmp_path / "worktree")
    assert "--clearenv" in tick and "--clearenv" in worker
    assert str(tmp_path / "github-token") in tick and str(tmp_path / "github-token") in worker
    for name in ("copilot.json", "release", "node"):
        assert str(tmp_path / name) in worker and str(tmp_path / name) not in tick
    assert str(tmp_path) not in tick and str(tmp_path) not in worker


def test_completion_is_reconciled_only_after_collection_and_survives_reopen(tmp_path, monkeypatch):
    fixture = Fixture(f"floe-e2e-tick-{uuid4().hex}", Path("/usr/bin/python3.14"), tmp_path / "state")
    fixture.initialize()
    manager = SmokeManager(fixture.state, None, tmp_path)
    monkeypatch.setattr(manager, "start", lambda *_: None)
    launcher, authority = fixture.launcher(manager)
    reservation = launcher.start_or_get(authority, REQUEST)
    monkeypatch.setattr(manager, "_show", lambda _: {"LoadState": "not-found", "ActiveState": "inactive"})
    assert launcher.status(authority, REQUEST.key).state is AttemptState.UNKNOWN
    io = manager.io(reservation)
    io.mkdir(mode=0o700)
    other = Reservation(reservation.key, reservation.run_id, reservation.unit_id, str(uuid4()))
    probe.write_outcome(io, other.run_id, other.session_id, "ok")
    with pytest.raises(WorkerUnavailable, match="smoke outcome does not match the reservation"):
        manager.inspect(reservation)
    probe.write_outcome(io, reservation.run_id, reservation.session_id, "ok")
    sessions = io / "sessions"
    sessions.mkdir(mode=0o700)
    with pytest.raises(WorkerUnavailable, match="native session not verified"):
        manager.inspect(reservation)
    native(sessions, reservation.session_id)
    with monkeypatch.context() as still_loaded:
        still_loaded.setattr(manager, "_show", lambda _: {
            "LoadState": "loaded", "Transient": "yes", "Description": manager._description(reservation),
            "ActiveState": "inactive",
        })
        assert launcher.status(authority, REQUEST.key).state is AttemptState.RUNNING
    collected = launcher.status(authority, REQUEST.key)
    assert collected.state is AttemptState.SUCCEEDED and not collected.slot_reserved
    (io / "outcome.json").unlink()
    launcher, authority = fixture.launcher(manager)
    assert launcher.status(authority, reservation.run_id) == collected


def test_provisioning_fails_closed_without_inputs(tmp_path):
    with pytest.raises(WorkerUnavailable, match="^required private smoke provisioning unavailable$"):
        SmokeInputs.load(tmp_path / "absent")


def test_provision_uses_one_existing_login_and_filters_pi_inputs(monkeypatch, tmp_path, capsys):
    agent = tmp_path / ".pi/agent"
    release = agent / "install/releases/1.5.2"
    executable = release / "node_modules/.bin/pi"
    executable.parent.mkdir(parents=True)
    executable.touch()
    auth = {"type": "oauth", "access": "synthetic-model-secret"}
    (agent / "auth.json").write_text(json.dumps({"github-copilot": auth, "other": "dropped"}))
    model = {"id": "gpt-6.2-sol", "provider": "github-copilot"}
    (agent / "models-store.json").write_text(json.dumps({
        "github-copilot": {"models": [{"id": "unrelated"}, model], "checkedAt": 1}, "other": {},
    }))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[1:])
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert "synthetic-login-token" not in argv
        if argv[1:3] == ["auth", "token"]:
            assert "GH_TOKEN" not in kwargs["env"]
            value = "synthetic-login-token\n"
        else:
            assert kwargs["env"]["GH_TOKEN"] == "synthetic-login-token"
            value = "maintainer\n"
        return subprocess.CompletedProcess(argv, 0, stdout=value.encode())

    monkeypatch.setattr(subprocess, "run", run)
    provision(tmp_path, Path("/usr/bin/true"))
    directory = tmp_path / ".local/state/floe-pi-smoke-inputs"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert {path.name for path in directory.iterdir()} == {
        "github-token", "copilot.json", "models-store.json", "config.json",
    }
    assert all(not path.is_symlink() and path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
    inputs = SmokeInputs.load(directory)
    assert inputs.tick_login == inputs.worker_login == "maintainer"
    assert inputs.release == release
    assert (directory / "github-token").read_text().strip() == "synthetic-login-token"
    assert json.loads((directory / "copilot.json").read_text()) == {"github-copilot": auth}
    assert json.loads((directory / "models-store.json").read_text()) == {
        "github-copilot": {"models": [model], "checkedAt": 1},
    }
    assert [call[:2] for call in calls] == [["auth", "token"], ["api", "--hostname"]]
    assert capsys.readouterr() == ("", "")
    with pytest.raises(WorkerUnavailable, match="already exists"):
        provision(tmp_path, Path("/usr/bin/true"))
    assert len(calls) == 2


def test_outcome_temporary_file_is_not_followed(tmp_path):
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("unchanged")
    (tmp_path / "outcome.tmp").symlink_to(unrelated)
    with pytest.raises(FileExistsError):
        probe.write_outcome(tmp_path, str(uuid4()), str(uuid4()), "ok")
    assert unrelated.read_text() == "unchanged"


def test_supervisor_does_not_follow_outcome_link(tmp_path):
    reservation = Reservation(REQUEST.key, str(uuid4()), "", str(uuid4()))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    probe.write_outcome(elsewhere, reservation.run_id, reservation.session_id, "ok")
    (tmp_path / "outcome.json").symlink_to(elsewhere / "outcome.json")
    with pytest.raises(WorkerUnavailable, match="smoke outcome unreadable"):
        read_outcome(tmp_path, reservation)


def wait_for_collection(manager, reservation, seconds=85):
    unit = manager.unit_name(reservation.run_id)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if manager._absent(manager._show(unit)):
            return
        time.sleep(0.05)
    pytest.fail("smoke unit did not collect", pytrace=False)


@pytest.mark.systemd_e2e
@pytest.mark.parametrize("role", ["tick", "worker"])
def test_installed_pi_smoke_boundary(tmp_path, role):
    # Exercise exactly the live service controls, but stop before any network
    # call: missing local auth must fail after the isolation checks pass.
    inputs = SmokeInputs(tmp_path, "reader", "maintainer", Path("/usr/bin/true"), tmp_path / "pi")
    inputs.release.mkdir()
    for name in ("github-token", "copilot.json", "models-store.json"):
        (tmp_path / name).touch(mode=0o600)
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    manager = SmokeManager(tmp_path, inputs, tmp_path)
    (manager.worktree / ".git").mkdir(mode=0o700, parents=True)
    reservation = Reservation(REQUEST.key, str(uuid4()), "", str(uuid4()))
    try:
        manager.dispatch(role, reservation, output)
        wait_for_collection(manager, reservation)
        outcome = read_outcome(output, reservation)
        if outcome != "required GitHub credential unavailable":
            pytest.fail(outcome or "smoke service has no verified outcome", pytrace=False)
    finally:
        if manager._owns(manager._show(manager.unit_name(reservation.run_id)), reservation):
            manager.stop(reservation)


@pytest.mark.systemd_e2e
def test_installed_pi_smoke(request):
    # Explicit, one-call opt-in. The ordinary full live suite cannot spend on Pi.
    if request.config.getoption("keyword") != "installed_pi_smoke":
        pytest.skip("budgeted smoke requires the exact trusted selection: -k installed_pi_smoke")
    try:
        inputs = live_inputs()
    except WorkerUnavailable as exc:
        pytest.fail(str(exc), pytrace=False)
    state = inputs.directory / f"run-{uuid4().hex}"
    fixture = Fixture(f"floe-e2e-tick-{uuid4().hex}", Path("/usr/bin/python3.14"), state)
    fixture.initialize()
    manager = SmokeManager(fixture.state, inputs, fixture.state)
    launcher, authority = fixture.launcher(manager)
    manager.worktree.mkdir(mode=0o700)
    try:
        initialized = subprocess.run(
            ["/usr/bin/git", "init", "--quiet", "--template=", str(manager.worktree)],
            env={**probe.ENV, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
        if initialized.returncode:
            pytest.fail("smoke Git worktree could not be initialized", pytrace=False)
        (manager.worktree / "README.md").write_text("Supervised Floe smoke fixture.\n")
        manager.tick()
        reservation = fixture.admit(launcher, authority)
        wait_for_collection(manager, reservation)
        finished = launcher.status(authority, reservation.run_id)
        if finished.state is not AttemptState.SUCCEEDED or finished.slot_reserved:
            outcome = read_outcome(manager.io(reservation), reservation)
            pytest.fail(outcome if outcome and outcome != "ok" else "smoke completion not verified", pytrace=False)
        reopened, caller = fixture.launcher(manager)
        if reopened.status(caller, REQUEST.key) != finished:
            pytest.fail("smoke launcher did not retain the terminal status", pytrace=False)
    except WorkerUnavailable as exc:
        pytest.fail(str(exc), pytrace=False)
    finally:
        try:
            try:
                status = launcher.status(authority, REQUEST.key)
            except AttemptNotFound:
                pass
            else:
                if status.slot_reserved:
                    cancelled = launcher.cancel(authority, status.reservation.run_id)
                    if cancelled.slot_reserved:
                        pytest.fail("smoke cleanup or completion remains unconfirmed", pytrace=False)
        finally:
            if manager.worktree.exists():
                shutil.rmtree(manager.worktree)
            if manager.worktree.exists():
                pytest.fail("smoke worktree cleanup not confirmed", pytrace=False)
