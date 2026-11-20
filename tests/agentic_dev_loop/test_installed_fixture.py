"""Wheel preflight (offline) and the leased, real-manager lifetime gate."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from floe import user_unit_dir

from tools.agentic_dev_loop.installed_fixture import Fixture, FixtureManager, REQUEST
from tools.agentic_dev_loop.launcher import AttemptNotFound, AttemptState, WorkerState, WorkerUnavailable


ROOT = Path(__file__).resolve().parents[2]


def command(*args, cwd=None, env=None, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=30)
    if check:
        assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result


def until(read, description, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if result := read():
            return result
        time.sleep(0.05)
    pytest.fail(f"did not observe {description} within {seconds}s")


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


@pytest.fixture(scope="module")
def wheel_python(tmp_path_factory):
    root = tmp_path_factory.mktemp("fixture-wheel").resolve()
    uv = shutil.which("uv") or Path.home() / ".local/bin/uv"
    command(uv, "build", "--wheel", "--offline", "--out-dir", root, ROOT, cwd=root)
    [wheel] = root.glob("floe-*.whl")
    env = root / "venv"
    command(uv, "venv", "--python", sys.executable, env, cwd=root)
    command(uv, "sync", "--project", ROOT, "--offline", "--locked", "--no-dev", "--no-install-project",
            cwd=root, env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(env)})
    python = env / "bin/python"
    command(uv, "pip", "install", "--offline", "--no-deps", "--python", python, wheel, cwd=root)
    return python


@pytest.fixture
def staged(tmp_path, wheel_python):
    protected = tmp_path / "protected"
    protected.mkdir()
    package = protected / "fixture_loop"
    package.mkdir()
    for module in ("__init__", "feature_tree", "readiness", "launcher", "tick", "installed_fixture"):
        shutil.copyfile(ROOT / f"tools/agentic_dev_loop/{module}.py", package / f"{module}.py")
    builder = tmp_path / "builder"
    builder.mkdir()
    for name in ("floe.py", "floe_cli.py", "fixture_loop.py", "workflow.py"):
        (builder / name).write_text('raise RuntimeError("broken Builder checkout")\n')
    fixture = Fixture(f"floe-e2e-tick-{uuid4().hex}", wheel_python, tmp_path / "state")
    fixture.initialize()

    def write(schedule, timeout_case=False):
        script = protected / "workflow.py"
        script.write_text(
            "from pathlib import Path\nfrom fixture_loop.installed_fixture import Fixture\n"
            f"WORKFLOW = {fixture.name!r}\nSCHEDULE = [{schedule!r}]\n"
            f"fixture = Fixture(WORKFLOW, Path({str(fixture.python)!r}), Path({str(fixture.state)!r}))\n"
            f"tasks = {{'reconcile': fixture.task(timeout_case={timeout_case!r})}}\n"
            "dag = {'reconcile': set()}\n"
        )
        for path in protected.rglob("*.py"):
            path.chmod(0o400)
        package.chmod(0o500)
        protected.chmod(0o500)
        return script

    def cli(*args, **kwargs):
        return command(fixture.python.with_name("floe"), *args, cwd=builder, **kwargs)

    try:
        yield fixture, write, cli, builder
    finally:
        protected.chmod(0o700)
        package.chmod(0o700)


def test_wheel_and_rendered_tick_outside_broken_builder(staged):
    fixture, write, cli, builder = staged
    script = write("hourly")
    assert "usage: floe" in cli("--help").stdout
    broken = command(fixture.python, "-m", "floe_cli", "--help", cwd=builder, check=False)
    assert broken.returncode != 0 and "broken Builder checkout" in broken.stderr
    dry_run = cli("install", "--dry-run", script).stdout
    start = next(line for line in dry_run.splitlines() if line.startswith("ExecStart="))
    argv = shlex.split(start.removeprefix("ExecStart="))
    assert argv == [str(fixture.python), "-m", "floe_cli", "run", str(script)]
    assert f"WorkingDirectory={script.parent}" in dry_run
    import_root = fixture.python.parent.parent / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    assert f'Environment="PYTHONPATH={import_root}"' in dry_run
    rendered = builder.parent / "rendered"
    rendered.mkdir()
    unit_files = []
    for suffix in ("service", "timer"):
        name = f"{fixture.name}.{suffix}"
        assert not (user_unit_dir() / name).exists()
        _, section = dry_run.split(f"# {user_unit_dir() / name} (create)\n", 1)
        path = rendered / name
        path.write_text(section.split(f"\n# {user_unit_dir()}/", 1)[0])
        unit_files.append(path)
    command("/usr/bin/systemd-analyze", "--user", "verify", *unit_files)
    assert {path.name for path in fixture.state.iterdir()} == {"ledger"}


def test_worker_stop_refuses_foreign_unit_and_missing_is_not_cleanup(tmp_path, monkeypatch):
    fixture = Fixture(f"floe-e2e-tick-{uuid4().hex}", Path(sys.executable), tmp_path / "state")
    fixture.initialize()
    manager = fixture.manager()
    monkeypatch.setattr(manager, "start", lambda *_: None)
    launcher, authority = fixture.launcher(manager)
    reservation = launcher.start_or_get(authority, REQUEST)
    stops = []
    monkeypatch.setattr(manager, "_systemctl", lambda *args: stops.append(args))
    monkeypatch.setattr(manager, "_show", lambda _: {"LoadState": "loaded", "Transient": "yes",
                                                    "Description": "a different reservation", "ActiveState": "active"})
    assert manager.inspect(reservation) is WorkerState.FOREIGN
    with pytest.raises(WorkerUnavailable):
        manager.stop(reservation)
    assert stops == []

    owned = {"LoadState": "loaded", "Transient": "yes", "Description": manager._description(reservation),
             "ActiveState": "active", "ControlGroup": f"/fixture/{FixtureManager.unit_name(reservation.run_id)}"}
    replaced = iter((owned, {**owned, "Description": "a different reservation"}))
    monkeypatch.setattr(manager, "_show", lambda _: next(replaced))
    stat = Path.stat
    with monkeypatch.context() as probe:
        probe.setattr(Path, "stat", lambda path, **kwargs: None if path.name == "cgroup.events" else stat(path, **kwargs))
        with pytest.raises(WorkerUnavailable, match="ownership changed"):
            manager.stop(reservation)
    assert stops == []

    monkeypatch.setattr(manager, "_show", lambda _: {"LoadState": "not-found", "ActiveState": "inactive"})
    assert manager.inspect(reservation) is WorkerState.MISSING
    assert launcher.cancel(authority, reservation.run_id).state is AttemptState.UNKNOWN


def systemctl(*args, **kwargs):
    return command("/usr/bin/systemctl", "--user", *args, **kwargs)


def history(cli, name):
    command("/usr/bin/journalctl", "--user", "--sync")
    return json.loads(cli("runs", "--name", name, "--json").stdout)


def invocation(fixture):
    paths = list(fixture.state.glob("tick-*.json"))
    if paths:
        [path] = paths
        return read_json(path)


def heartbeat(fixture, tick):
    worker = read_json(fixture.state / "heartbeat.json")
    assert worker and worker["run_id"] == tick["run_id"]
    assert worker["cgroup"] != tick["cgroup"]
    assert FixtureManager.unit_name(tick["run_id"]) in worker["cgroup"]
    assert f"{fixture.name}.service" in tick["cgroup"]
    return worker


def advances(fixture, tick, before):
    def progressed():
        worker = heartbeat(fixture, tick)
        assert worker["pid"] == before["pid"]
        return worker["sequence"] > before["sequence"]
    until(progressed, "the same worker making progress after the tick ended")


def cleanup(cli, fixture, launcher, authority):
    # Stop and uninstall the tick first so it cannot make another admission,
    # but still attempt worker cleanup if uninstall itself fails.
    try:
        cli("uninstall", "--name", fixture.name)
    finally:
        try:
            status = launcher.status(authority, REQUEST.key)
        except AttemptNotFound:
            pass
        else:
            status = launcher.cancel(authority, status.reservation.run_id)
            assert status.state is AttemptState.CANCELLED and not status.slot_reserved
            assert not systemctl("list-units", "--all", "--plain", "--no-legend",
                                 FixtureManager.unit_name(status.reservation.run_id)).stdout.strip()


@pytest.mark.systemd_e2e
@pytest.mark.parametrize("timeout_case", [False, True], ids=["timer-and-join", "timeout-and-recover"])
def test_installed_tick_lifetime(staged, timeout_case):
    fixture, write, cli, _ = staged
    # One future activation; unlike a recurring timer this cannot enqueue a second tick.
    date = datetime.now(timezone.utc) + (timedelta(days=1) if timeout_case else timedelta(seconds=15))
    script = write(date.strftime("%Y-%m-%d %H:%M:%S UTC"), timeout_case)
    release = fixture.state / "release"
    launcher, authority = fixture.launcher()
    unit = f"{fixture.name}.service"
    try:
        cli("install", "--timeout", "6s" if timeout_case else "25s", script)
        if timeout_case:
            systemctl("start", "--no-block", unit)
        tick = until(lambda: invocation(fixture), "installed tick admitting the fixture", seconds=90)
        until(lambda: read_json(fixture.state / "heartbeat.json"), "worker heartbeat")
        worker = heartbeat(fixture, tick)
        assert Path(f"/proc/{tick['pid']}/cmdline").read_bytes().split(b"\0") == [
            os.fsencode(fixture.python), b"-m", b"floe_cli", b"run", os.fsencode(script), b""]
        if not timeout_case:
            timer = systemctl("show", f"{fixture.name}.timer", "-p", "LastTriggerUSec", "--value").stdout.strip()
            assert timer and timer != "n/a"
            with ExitStack() as stack:
                starts = []
                for i in range(2):
                    log = stack.enter_context((fixture.state / f"start-{i}.log").open("w+"))
                    process = subprocess.Popen(["/usr/bin/systemctl", "--user", "--show-transaction", "start", unit],
                                               stdout=log, stderr=subprocess.STDOUT)
                    stack.callback(reap, process)
                    starts.append((process, Path(log.name)))
                job = systemctl("show", unit, "-p", "Job", "--value").stdout.strip()
                assert job.isdecimal() and int(job) > 0
                for process, log in starts:
                    until(lambda: f"Enqueued anchor job {job} {unit}/start." in log.read_text(),
                          "manual start joining the timer job")
                    assert process.poll() is None
                release.touch()
                assert [process.wait(timeout=10) for process, _ in starts] == [0, 0]
        else:
            child = Path(f"/proc/{tick['child_pid']}")
            assert child.joinpath("cgroup").read_text() == tick["cgroup"]
            until(lambda: not child.exists(), "tick-local child being reaped")
            assert systemctl("show", unit, "-p", "Result", "--value").stdout.strip() == "timeout"

        [run] = history(cli, fixture.name)
        assert run["run_id"] == tick["invocation"]
        assert run["status"] == ("incomplete" if timeout_case else "succeeded")
        if timeout_case:
            assert run["result"] == "timeout"
        assert not Path(f"/proc/{tick['pid']}").exists()
        after_exit = heartbeat(fixture, tick)
        assert after_exit["pid"] == worker["pid"]
        advances(fixture, tick, after_exit)
        assert launcher.status(authority, REQUEST.key).state is AttemptState.RUNNING
        if timeout_case:
            release.touch()
            cli("start", "--name", fixture.name)
            launcher, authority = fixture.launcher()
            [second] = [read_json(path) for path in fixture.state.glob("tick-*.json")
                        if path.stem != f"tick-{tick['invocation']}"]
            assert second["invocation"] != tick["invocation"]
            assert second["run_id"] == tick["run_id"]
            assert second["session_id"] == tick["session_id"]
        assert (fixture.state / "launches").read_text().splitlines() == [tick["run_id"]]
        cancelled = launcher.cancel(authority, tick["run_id"])
        assert cancelled.state is AttemptState.CANCELLED and not cancelled.slot_reserved
        reopened, new_authority = fixture.launcher()
        assert reopened.cancel(new_authority, REQUEST.key) == cancelled
        assert reopened.status(new_authority, tick["run_id"]) == cancelled
        assert reopened.start_or_get(new_authority, REQUEST) == cancelled.reservation
        assert (fixture.state / "launches").read_text().splitlines() == [tick["run_id"]]
        assert not Path(f"/proc/{worker['pid']}").exists()
    finally:
        cleanup(cli, fixture, launcher, authority)


def reap(process):
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)
