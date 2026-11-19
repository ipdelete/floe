"""Exercise real bootstrap/worker and lifecycle gate; fake only cgroupfs I/O."""

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path

import pytest

import floe
from floe import cgroups
from floe.results import RunResults
from floe.scheduler import TaskSchedule
from floe.worker import observe_managed_worker


class KernelGroup:
    def __init__(self, path):
        self.path = path
        self.procs = path / 'membership'
        self.procs.touch()
        self.linger = self.busy = False
        self.killed = self.removed = False
        self.at_exit = asyncio.Event()
        self.release = asyncio.Event()
        self.deny_join = False

    def open_control(self, name, flags):
        if name == 'cgroup.kill':
            return os.open(os.devnull, flags)
        assert name == 'cgroup.procs'
        self.busy = self.linger
        return os.open('/dev/full' if self.deny_join else self.procs, flags)

    def populated(self):
        return self.busy

    async def wait_empty(self):
        self.at_exit.set()
        if self.busy:
            await self.release.wait()
        assert not self.busy

    def kill(self):
        self.killed = True
        self.busy = False
        self.release.set()

    def remove_empty(self):
        if self.busy:
            raise floe.FloeError('task group still populated')
        self.removed = True


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    g = KernelGroup(tmp_path)
    @contextmanager
    def create():
        yield g
    monkeypatch.setattr(cgroups, '_new_group', create)
    return g


def workflow(tmp_path):
    path = tmp_path / 'flow.py'
    path.write_text('''
import os
from pathlib import Path
root = Path(__file__).parent
if os.environ.get('FLOE_RUN_ID') == 'gated':
    (root / 'loaded').write_bytes((root / 'membership').read_bytes())
WORKFLOW = 'demo'
def a():
    print('ran A')
    return 17
def c(x):
    print(x)
tasks = {'A': a, 'C': c}
dag = dict.fromkeys(tasks, ())
inputs = {'C': {'x': 'A'}}
''')
    return floe.load(path)


def call(w, results, send, *, drain=3):
    return observe_managed_worker(w, 'A', results, 'gated', send,
                                  timeout=8, drain_timeout=drain, cleanup_timeout=1)


def test_exit_and_eof_do_not_release_dependent_while_populated(tmp_path, kernel):
    w = workflow(tmp_path)
    schedule, results = TaskSchedule(w, 2), RunResults(w.inputs)
    assert schedule.next_task() == 'A'
    kernel.linger = True  # model a surviving descendant; no further output
    output = bytearray()
    saw_output = asyncio.Event()
    def send(task, data):
        output.extend(data)
        if output.endswith(b'\n'):
            saw_output.set()

    async def drive():
        observation = asyncio.create_task(call(w, results, send))
        await asyncio.wait_for(kernel.at_exit.wait(), 4)
        await asyncio.wait_for(saw_output.wait(), 2)
        assert output == b'ran A\n' and not observation.done()
        assert schedule.next_task() is None and not results.values
        kernel.busy = False
        kernel.release.set()
        evidence = await observation
        assert kernel.removed and not kernel.killed
        assert (evidence.value, evidence.returncode, evidence.error) == (b'17', 0, None)
        results.commit_encoded('A', evidence.value)
        schedule.finish('A', 'succeeded')
        assert schedule.next_task() == 'C' and results.arguments('C') == {'x': 17}
    asyncio.run(drive())
    assert (tmp_path / 'loaded').read_bytes() == b'0'  # joined before definition load


def test_drain_timeout_kills_owned_group_and_discards_staged_success(tmp_path, kernel):
    w = workflow(tmp_path)
    kernel.linger = True
    with pytest.raises(TimeoutError):
        asyncio.run(call(w, RunResults(w.inputs), lambda *_: None, drain=.04))
    assert kernel.at_exit.is_set() and kernel.killed and kernel.removed


def test_cancel_during_drain_cleans_group_without_returning_bytes(tmp_path, kernel):
    w = workflow(tmp_path)
    kernel.linger = True
    async def drive():
        observation = asyncio.create_task(call(w, RunResults(w.inputs), lambda *_: None))
        await asyncio.wait_for(kernel.at_exit.wait(), 4)
        observation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observation
    asyncio.run(drive())
    assert kernel.killed and kernel.removed


def test_failed_placement_never_loads_definition(tmp_path, kernel):
    kernel.deny_join = True
    w = workflow(tmp_path)
    evidence = asyncio.run(call(w, RunResults(w.inputs), lambda *_: None))
    assert evidence.returncode != 0 and evidence.error and evidence.value is None
    assert kernel.removed and not (tmp_path / 'loaded').exists()


def test_unknown_population_blocks_success_even_if_later_cleanup_works(tmp_path, kernel, monkeypatch):
    normal = kernel.wait_empty
    async def query():
        if not kernel.killed:
            raise floe.FloeError('population unknown')
        await normal()
    monkeypatch.setattr(kernel, 'wait_empty', query)
    w = workflow(tmp_path)
    with pytest.raises(floe.FloeError, match='population unknown'):
        asyncio.run(call(w, RunResults(w.inputs), lambda *_: None))
    assert kernel.killed and kernel.removed


def test_failed_cleanup_reports_owned_path_and_does_not_remove(tmp_path, kernel, monkeypatch):
    def cannot_kill():
        raise PermissionError('denied')
    monkeypatch.setattr(kernel, 'kill', cannot_kill)
    w = workflow(tmp_path)
    kernel.linger = True
    with pytest.raises(floe.FloeError, match='cleanup unconfirmed') as err:
        asyncio.run(call(w, RunResults(w.inputs), lambda *_: None, drain=.04))
    assert all(text in str(err.value) for text in (str(tmp_path), 'TimeoutError', 'denied'))
    assert not kernel.removed


def test_failed_final_removal_discards_success_even_when_cleanup_recovers(tmp_path, kernel, monkeypatch):
    remove = kernel.remove_empty
    def fail_once():
        if not kernel.killed:
            raise PermissionError('first removal failed')
        remove()
    monkeypatch.setattr(kernel, 'remove_empty', fail_once)
    w = workflow(tmp_path)
    with pytest.raises(PermissionError, match='first removal failed'):
        asyncio.run(call(w, RunResults(w.inputs), lambda *_: None))
    assert kernel.killed and kernel.removed


@pytest.mark.parametrize('content', ['', 'populated', 'populated 2', 'populated 0\npopulated 1'])
def test_population_must_be_known_and_unique(tmp_path, content):
    group = tmp_path / 'owned'
    group.mkdir()
    (group / 'cgroup.events').write_text(content)
    p, g = os.open(tmp_path, os.O_DIRECTORY), os.open(group, os.O_DIRECTORY)
    try:
        with pytest.raises(floe.FloeError, match='unknown population'):
            cgroups._OwnedGroup(group, p, g).populated()
    finally:
        os.close(g)
        os.close(p)


def test_non_cgroup_mount_rejected_before_mkdir(tmp_path, monkeypatch):
    monkeypatch.setattr(cgroups, '_current_root', lambda: tmp_path)
    with pytest.raises(floe.FloeError, match='not a verified cgroup v2 mount'):
        with cgroups._new_group():
            pytest.fail('must not launch')
    assert list(tmp_path.iterdir()) == []


def test_initial_nonempty_group_is_not_killed(tmp_path, kernel):
    kernel.busy = True
    async def drive():
        async with cgroups.managed_group(1):
            pytest.fail('must not launch')
    with pytest.raises(floe.FloeError, match='populated'):
        asyncio.run(drive())
    assert not kernel.killed and not kernel.removed


def test_replaced_group_never_killed_or_removed(tmp_path):
    path = tmp_path / 'owned'
    path.mkdir()
    p, g = os.open(tmp_path, os.O_DIRECTORY), os.open(path, os.O_DIRECTORY)
    try:
        owned = cgroups._OwnedGroup(path, p, g)
        path.rename(tmp_path / 'original')
        path.mkdir()
        (path / 'cgroup.kill').write_text('foreign')
        for action in (owned.kill, owned.remove_empty):
            with pytest.raises(floe.FloeError, match='replaced'):
                action()
        assert (path / 'cgroup.kill').read_text() == 'foreign'
    finally:
        os.close(g)
        os.close(p)
