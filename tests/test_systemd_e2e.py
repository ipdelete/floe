"""Opt-in tests against the real user systemd session and journald.

    uv run pytest -q -m systemd_e2e --systemd-e2e

Every test deploys its own generated workflow, named floe-e2e-<random>, from a
temporary directory, and uninstalls it afterward. Units with that prefix that
an interrupted session left behind are removed before and after the module.
Nothing else in the user session is touched. Normal pytest runs skip this module.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

import floe

pytestmark = pytest.mark.systemd_e2e

PREFIX = "floe-e2e-"
FLOE = Path(sys.executable).parent / "floe"


# --- Helpers -------------------------------------------------------------------

def floe_command(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """The real `floe` command from this environment, as a user would run it."""
    result = subprocess.run([str(FLOE), *args], capture_output=True, text=True, timeout=120)
    if check:
        assert result.returncode == 0, f"floe {' '.join(args)}:\n{result.stdout}{result.stderr}"
    return result


def floe_json(*args: str, check: bool = True):
    return json.loads(floe_command(*args, "--json", check=check).stdout)


def listed(name: str, *, verify: bool = False) -> dict:
    """This workflow's `floe list` entry. With verify, `--check`, whose exit
    status also reflects other installed workflows, so it is not asserted."""
    [entry] = [w for w in floe_json("list", *(["--check"] if verify else []), check=not verify)
               if w["name"] == name]
    return entry


def sync_journal() -> None:
    subprocess.run(["journalctl", "--user", "--sync"], check=True, capture_output=True, timeout=30)


def runs(name: str, n: int = 5) -> list[dict]:
    sync_journal()
    return floe_json("runs", name, "-n", str(n))


def wait_for(predicate, what: str, timeout: float = 30, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    while True:
        if value := predicate():
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(interval)


def store() -> floe.UnitStore:
    return floe.UnitStore(floe.user_unit_dir())


def stamp(name: str) -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "systemd" / "timers" / f"stamp-{name}.timer"


def assert_clean(name: str) -> None:
    """No files, enable links, loaded units, timer stamp, or `floe list` entry."""
    units, sd = store(), floe.Systemd()
    for unit in (f"{name}.service", f"{name}.timer"):
        file = units.path / unit
        assert not (file.exists() or file.is_symlink()), f"{file} left behind"
        state = sd.show(unit, "LoadState", "ActiveState")
        assert state == {"LoadState": "not-found", "ActiveState": "inactive"}, (unit, state)
        for suffix in ("wants", "requires", "upholds"):
            for directory in units.path.glob(f"*.{suffix}"):
                assert not (directory / unit).is_symlink(), f"{directory / unit} left behind"
    # A leftover stamp would make the next install "catch up" with a run at once.
    assert not stamp(name).exists(), f"{stamp(name)} left behind"
    assert name not in [w["name"] for w in floe_json("list")]


def leftover_names() -> set[str]:
    """floe-e2e-* units with files in the unit directory or loaded in systemd."""
    names = {file.name.rsplit(".", 1)[0] for file in store().path.glob(f"{PREFIX}*")
             if file.name.endswith((".service", ".timer"))}
    listed = subprocess.run(
        ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", f"{PREFIX}*"],
        capture_output=True, text=True, timeout=30,
    ).stdout
    names |= {line.split()[0].rsplit(".", 1)[0] for line in listed.splitlines() if line.strip()}
    return names


def remove_leftovers() -> None:
    for name in sorted(leftover_names()):
        for unit in (f"{name}.service", f"{name}.timer"):
            if (store().path / unit).is_symlink() and (store().path / unit).resolve() == Path("/dev/null"):
                subprocess.run(["systemctl", "--user", "unmask", unit], capture_output=True, timeout=30)
        floe_command("uninstall", name, check=False)
        assert_clean(name)


@dataclass
class Flow:
    name: str
    script: Path

    @property
    def service(self) -> str:
        return f"{self.name}.service"

    @property
    def timer(self) -> str:
        return f"{self.name}.timer"

    def write(self, body: str, schedule: str = '["hourly"]') -> None:
        self.script.write_text(f"WORKFLOW = {self.name!r}\nSCHEDULE = {schedule}\n\n{textwrap.dedent(body)}")

    def install(self, *args: str) -> str:
        return floe_command("install", *args, str(self.script)).stdout


# --- Fixtures ------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def no_leftovers():
    remove_leftovers()
    yield
    remove_leftovers()


@pytest.fixture
def workflow(tmp_path):
    """Make generated workflows; uninstall each and verify cleanup afterward."""
    flows = []

    def make(body: str, *, schedule: str = '["hourly"]', directory: str = "flows",
             install: bool = True) -> Flow:
        name = f"{PREFIX}{uuid.uuid4().hex[:8]}"
        script = (tmp_path / name / directory / "flow.py")
        script.parent.mkdir(parents=True)
        flow = Flow(name, script.resolve())
        flows.append(flow)
        flow.write(body, schedule)
        if install:
            flow.install()
        return flow

    yield make
    failures = []
    for flow in flows:
        result = floe_command("uninstall", flow.name, check=False)
        try:
            assert_clean(flow.name)
            assert result.returncode == 0, result.stdout + result.stderr
        except AssertionError as exc:
            failures.append(f"{flow.name}: {exc}")
    assert not failures, "\n".join(failures)


SUCCESS = '''
    import sys
    from helper import greeting

    def fetch():
        print(greeting("fetch"), file=sys.stderr)

    def transform():
        print("transforming", file=sys.stderr)

    tasks = {"fetch": fetch, "transform": transform}
    dag = {"fetch": set(), "transform": {"fetch"}}
'''

SIMPLE = SUCCESS.replace("from helper import greeting", "greeting = str")

BLOCKING = '''
    import time
    from pathlib import Path

    RELEASE = Path(__file__).with_name("release")

    def wait():
        Path(__file__).with_name("started").touch()
        deadline = time.monotonic() + 60
        while not RELEASE.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

    tasks = {"wait": wait}
    dag = {"wait": set()}
'''


# --- Deployment and a successful run --------------------------------------------

def test_install_start_and_history(workflow):
    # Space and % in the path check quoting in the generated units.
    flow = workflow(SUCCESS, directory="my flows 100%", install=False)
    (flow.script.parent / "helper.py").write_text("def greeting(task):\n    return f'hello from {task}'\n")
    sd, units = floe.Systemd(), store()

    assert "(create)" in floe_command("install", "--dry-run", str(flow.script)).stdout
    assert not (units.path / flow.service).exists()
    assert sd.show(flow.service, "LoadState")["LoadState"] == "not-found"

    flow.install()
    assert runs(flow.name) == [], "install triggered a run"
    expected = floe.Deployment(flow.name, flow.script, ("hourly",)).render_units()
    for unit, text in expected.items():
        assert (units.path / unit).read_text() == text
    assert sd.show(flow.timer, "ActiveState", "UnitFileState") == {
        "ActiveState": "active", "UnitFileState": "enabled"}
    assert (units.path / "timers.target.wants" / flow.timer).resolve() == units.path / flow.timer
    entry = listed(flow.name, verify=True)
    assert (entry["script"], entry["problems"], entry["enabled"]) == (str(flow.script), [], True)
    assert entry["next_run"]

    mtimes = {unit: (units.path / unit).stat().st_mtime_ns for unit in expected}
    assert "nothing (up to date)" in flow.install()
    assert mtimes == {unit: (units.path / unit).stat().st_mtime_ns for unit in expected}

    floe_command("start", flow.name)
    service = sd.show(flow.service, "Result", "ExecMainStatus", "InvocationID")
    assert (service["Result"], service["ExecMainStatus"]) == ("success", "0")
    invocation = service["InvocationID"]
    assert "ActiveState=inactive" in floe_command("status", flow.name).stdout

    # The events reach journald as JSON lines, independent of floe's parsing.
    sync_journal()
    journal = subprocess.run(
        ["journalctl", "--user", "-u", flow.service, f"_SYSTEMD_INVOCATION_ID={invocation}",
         "-o", "cat", "--no-pager"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout
    events = [json.loads(line) for line in journal.splitlines() if line.startswith("{")]
    assert [e["event"] for e in events] == [
        "run_started", "task_started", "task_completed", "task_started", "task_completed", "run_completed"]
    assert {e["run_id"] for e in events} == {invocation}

    # History, as floe reconstructs it from the same records.
    [run] = runs(flow.name)
    assert (run["run_id"], run["status"]) == (invocation, "succeeded")
    assert [(t["name"], t["status"]) for t in run["tasks"]] == [("fetch", "succeeded"), ("transform", "succeeded")]
    logs = floe_json("logs", flow.name, invocation[:8])
    assert logs["run"]["run_id"] == invocation
    kinds = [line["kind"] for line in logs["lines"]]
    assert kinds[0] == kinds[-1] == "systemd"
    output = [(line["task"], line["message"]) for line in logs["lines"] if line["kind"] == "output"]
    assert output == [("fetch", "hello from fetch"), ("transform", "transforming")]
    assert "■ run succeeded" in floe_command("logs", flow.name).stdout


# --- How runs end -----------------------------------------------------------------

def test_task_stdout_is_attributed_to_its_task(workflow):
    """print() and child processes writing to stdout land in journald as output
    of the task that wrote them, not after it or as part of the event stream."""
    flow = workflow('''
        import subprocess

        def first():
            print("printed by first")
            subprocess.run(["echo", "child of first"], check=True)

        def second():
            print("printed by second")

        tasks = {"first": first, "second": second}
        dag = {"first": set(), "second": {"first"}}
    ''')
    floe_command("start", flow.name)
    sync_journal()
    logs = floe_json("logs", flow.name)
    assert logs["run"]["status"] == "succeeded"
    assert [(t["name"], t["status"]) for t in logs["run"]["tasks"]] == [
        ("first", "succeeded"), ("second", "succeeded")]
    output = [(line["task"], line["message"]) for line in logs["lines"] if line["kind"] == "output"]
    assert output == [("first", "printed by first"), ("first", "child of first"),
                      ("second", "printed by second")]

@pytest.mark.parametrize("eventual_success", [True, False])
def test_task_retries_preserve_attempt_history_and_output(workflow, eventual_success):
    flow = workflow(f'''
        from floe import RetryPolicy

        count = 0

        def fetch():
            global count
            count += 1
            print(f"attempt output {{count}}")
            if count == 1 or not {eventual_success!r}:
                raise TimeoutError("transient")

        tasks = {{"fetch": fetch}}
        dag = {{"fetch": set()}}
        retries = {{"fetch": RetryPolicy(2, 0, (TimeoutError,))}}
    ''')
    result = floe_command("start", flow.name, check=False)
    assert result.returncode == (0 if eventual_success else 1)
    sync_journal()
    logs = floe_json("logs", flow.name)
    task, = logs["run"]["tasks"]
    expected = "succeeded" if eventual_success else "failed"
    assert task["status"] == logs["run"]["status"] == expected
    assert [(a["number"], a["status"]) for a in task["attempts"]] == [(1, "failed"), (2, expected)]
    assert task["failure_reason"] == (None if eventual_success else "exhausted")
    assert [(line["message"], line["attempt"]) for line in logs["lines"] if line["kind"] == "output"] == [
        ("attempt output 1", 1), ("attempt output 2", 2)]


def test_failed_task(workflow):
    flow = workflow('''
        def ok():
            pass

        def boom():
            raise RuntimeError("broken input")

        def after():
            pass

        tasks = {"ok": ok, "boom": boom, "after": after}
        dag = {"ok": set(), "boom": {"ok"}, "after": {"boom"}}
    ''')
    result = floe_command("start", flow.name, check=False)
    assert result.returncode == 1
    assert f"see: floe logs {flow.name}" in result.stderr
    assert floe.Systemd().show(flow.service, "Result")["Result"] == "exit-code"
    [run] = runs(flow.name)
    assert (run["status"], run["error"]) == ("failed", "broken input")
    assert [(t["name"], t["status"]) for t in run["tasks"]] == [("ok", "succeeded"), ("boom", "failed")]
    # Teardown checks that uninstall resets the failed state.


def test_invalid_definition(workflow):
    flow = workflow(SIMPLE)
    flow.write('''
        def a():
            pass

        tasks = {"a": a}
        dag = {"a": {"missing"}}
    ''')
    assert floe_command("start", flow.name, check=False).returncode == 1
    [run] = runs(flow.name)
    assert (run["status"], run["tasks"]) == ("failed", [])
    assert "unknown prerequisites: 'missing'" in run["error"]


def test_timeout_kills_the_whole_run(workflow):
    flow = workflow('''
        import subprocess
        import time
        from pathlib import Path

        def hang():
            child = subprocess.Popen(["sleep", "300"])
            Path(__file__).with_name("child.pid").write_text(str(child.pid))
            time.sleep(300)

        tasks = {"hang": hang}
        dag = {"hang": set()}
    ''', install=False)
    deployment = floe.Deployment.for_workflow(floe.load(flow.script), timeout="3s")
    floe.install(deployment, store(), floe.Systemd())

    assert floe_command("start", flow.name, check=False).returncode == 1
    [run] = runs(flow.name)
    assert (run["status"], run["result"]) == ("incomplete", "timeout")
    assert [(t["name"], t["status"]) for t in run["tasks"]] == [("hang", "incomplete")]

    # KillMode=control-group: processes a task started die with it.
    pid = int((flow.script.parent / "child.pid").read_text())

    def gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False
    wait_for(gone, f"task's child process {pid} to be killed", timeout=10)


def test_running_and_no_overlap(workflow):
    flow = workflow(BLOCKING)
    sd = floe.Systemd()
    floe.start(flow.name, sd, wait=False)
    wait_for((flow.script.parent / "started").exists, "the task to start")
    [run] = wait_for(lambda: [r for r in runs(flow.name) if r["status"] == "running"], "a running run")
    assert run["tasks"] == [{**run["tasks"][0], "name": "wait", "status": "running"}]

    # Starting a running oneshot service joins the current run.
    invocation = sd.show(flow.service, "InvocationID")["InvocationID"]
    floe.start(flow.name, sd, wait=False)
    assert sd.show(flow.service, "InvocationID")["InvocationID"] == invocation

    (flow.script.parent / "release").touch()
    wait_for(lambda: sd.show(flow.service, "ActiveState")["ActiveState"] == "inactive", "the run to finish")
    assert [(r["run_id"], r["status"]) for r in runs(flow.name)] == [(invocation, "succeeded")]


def test_uninstall_stops_a_running_run(workflow):
    flow = workflow(BLOCKING)
    floe.start(flow.name, floe.Systemd(), wait=False)
    wait_for((flow.script.parent / "started").exists, "the task to start")
    floe_command("uninstall", flow.name)
    assert_clean(flow.name)
    [run] = runs(flow.name)
    assert run["status"] == "incomplete"


# --- Reinstalling and drift ---------------------------------------------------------

def test_schedule_change_needs_reinstall(workflow):
    flow = workflow(SIMPLE)
    flow.write(SIMPLE, schedule='["daily"]')

    assert floe_command("list", "--check", check=False).returncode == 1
    assert listed(flow.name, verify=True)["problems"] == [f"units out of date ({flow.timer}); reinstall"]

    # By name: uses the definition file recorded at install time.
    assert floe_command("install", flow.name).stdout.startswith(f"wrote: {flow.timer};")
    entry = listed(flow.name, verify=True)
    assert (entry["schedule"], entry["problems"]) == (["*-*-* 00:00:00"], [])
    floe_command("run", flow.name)


def test_missing_script_and_uninstall_by_name(workflow):
    flow = workflow(SIMPLE)
    flow.script.unlink()
    assert f"script not found: {flow.script}" in listed(flow.name)["problems"]
    assert floe_command("uninstall", flow.name).stdout == f"removed: {flow.timer}, {flow.service}\n"
    assert_clean(flow.name)


def test_uninstall_continues_after_interruption(workflow):
    flow = workflow(SIMPLE)
    units, sd = store(), floe.Systemd()
    # As if a previous uninstall died after deleting files, before daemon-reload.
    (units.path / "timers.target.wants" / flow.timer).unlink()
    for unit in (flow.service, flow.timer):
        (units.path / unit).unlink()
    assert sd.show(flow.timer, "LoadState", "ActiveState") == {"LoadState": "loaded", "ActiveState": "active"}
    floe_command("uninstall", flow.name)
    assert_clean(flow.name)


# --- Ownership -------------------------------------------------------------------------

FOREIGN = "[Unit]\nDescription=not floe\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n"


def test_foreign_unit_needs_adopt(workflow):
    flow = workflow(SIMPLE, install=False)
    units = store()
    units.path.mkdir(parents=True, exist_ok=True)
    (units.path / flow.service).write_text(FOREIGN)
    try:
        result = floe_command("install", str(flow.script), check=False)
        assert result.returncode == 1 and "adopt to take over" in result.stderr
        assert (units.path / flow.service).read_text() == FOREIGN
        assert not (units.path / flow.timer).exists(), "a unit was written despite the conflict"

        flow.install("--adopt")
        assert units.is_owned(flow.service) and units.is_owned(flow.timer)
    finally:
        if not units.is_owned(flow.service):  # uninstall leaves foreign files alone
            (units.path / flow.service).unlink(missing_ok=True)


def test_masked_unit_is_never_overwritten(workflow):
    flow = workflow(SIMPLE, install=False)
    units = store()
    subprocess.run(["systemctl", "--user", "mask", flow.timer], check=True, capture_output=True, timeout=30)
    try:
        result = floe_command("install", "--adopt", str(flow.script), check=False)
        assert result.returncode == 1 and "masked or linked" in result.stderr
        assert (units.path / flow.timer).resolve() == Path("/dev/null")
        assert not (units.path / flow.service).exists(), "a unit was written despite the conflict"
    finally:
        subprocess.run(["systemctl", "--user", "unmask", flow.timer], check=True, capture_output=True, timeout=30)


# --- Scheduling ----------------------------------------------------------------------------

def test_timer_starts_runs(workflow):
    """Slow: the timer's default AccuracySec=1min can delay the first activation."""
    flow = workflow(SIMPLE, schedule='["*:*:0/5"]')
    [run] = wait_for(lambda: [r for r in runs(flow.name) if r["status"] == "succeeded"],
                     "a timer-started run", timeout=90, interval=2)
    assert [t["name"] for t in run["tasks"]] == ["fetch", "transform"]
