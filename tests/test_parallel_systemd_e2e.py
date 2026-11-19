"""Product acceptance through Floe CLI, generated service, kernel and journal."""

from contextlib import contextmanager
import os
from pathlib import Path
import select
import subprocess
import sys
import uuid

import pytest

from test_systemd_e2e import FLOE, Flow, assert_clean, floe_command, floe_json, store, sync_journal, wait_for

pytestmark = pytest.mark.systemd_e2e

COMMON = '''
from pathlib import Path
import os, subprocess, sys, time
from floe import parallel

def wait(name):
    until = time.monotonic() + 10
    while not Path(name).exists():
        if time.monotonic() > until:
            raise TimeoutError(name)
        time.sleep(.01)

def meet(name, peer):
    Path(name).touch()
    wait(peer)
'''


@pytest.fixture
def installed(tmp_path):
    flow = Flow(f'floe-e2e-parallel-{uuid.uuid4().hex}', tmp_path / 'flow.py')
    def install(body, *args):
        # Avoid calendar triggering these fixtures even if a test crosses an hour.
        flow.write(body, '["2099-01-01 00:00:00"]')
        flow.install('--workers', '2', *args)
        return flow
    yield install
    if flow.script.exists():
        floe_command('uninstall', flow.name)
        assert_clean(flow.name)


@contextmanager
def running(flow):
    proc = subprocess.Popen([str(FLOE), 'start', flow.name], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            # The fixture has bounded waits; don't leave a start client behind.
            try:
                proc.communicate(timeout=14)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.communicate(timeout=5)


def history(flow):
    sync_journal()
    return floe_json('logs', flow.name)


def statuses(log):
    return {entry['name']: entry['status'] for entry in log['run']['tasks']}


def test_installed_parallel_join_and_output_attribution(installed):
    flow = installed(COMMON + '''
@parallel
def a():
    meet('A.ready', 'B.ready')
    os.write(2, b'prices from A\\n')
    return {'apple': 7}
@parallel
def b():
    meet('B.ready', 'A.ready')
    subprocess.run([sys.executable, '-c', 'print("quantities from child B")'], check=True, timeout=4)
    return {'apple': 3}
def c(*, quantities, prices):
    Path('total').write_text(str(prices['apple'] * quantities['apple']))
tasks = {'A': a, 'B': b, 'C': c}
dag = dict.fromkeys(tasks, ())
inputs = {'C': {'prices': 'A', 'quantities': 'B'}}
''')
    floe_command('start', flow.name)
    assert (flow.script.parent / 'total').read_text() == '21'
    log = history(flow)
    assert log['run']['status'] == 'succeeded' and statuses(log) == dict.fromkeys('ABC', 'succeeded')
    assert {(line['task'], line['message']) for line in log['lines'] if line['kind'] == 'output' and line['task']} == {
        ('A', 'prices from A'), ('B', 'quantities from child B')}


@pytest.mark.parametrize('mode', ['task', 'workflow', 'failure'])
def test_installed_cancel_and_failure_matrix(installed, mode):
    fail = "wait('fail'); raise ValueError('controlled failure')" if mode == 'failure' else "wait('unused')"
    flow = installed(COMMON + f'''
@parallel
def a():
    meet('A.ready', 'B.ready')
    {fail}
@parallel
def b():
    meet('B.ready', 'A.ready')
    wait('release-B')
    return 17
@parallel
def q():
    Path('Q.ran').touch()
def c(*, item):
    Path('C.ran').touch()
def d(*, item):
    Path('D.result').write_text(str(item))
tasks = {{'A': a, 'B': b, 'Q': q, 'C': c, 'D': d}}
dag = dict.fromkeys(tasks, ())
inputs = {{'C': {{'item': 'A'}}, 'D': {{'item': 'B'}}}}
''')
    directory = flow.script.parent
    with running(flow) as proc:
        wait_for(lambda: (directory / 'A.ready').exists() and (directory / 'B.ready').exists(), 'both tasks')
        if mode == 'failure':
            (directory / 'fail').touch()
        else:
            floe_command('cancel', flow.name, *(['--task', 'A'] if mode == 'task' else []))
        if mode != 'workflow':
            expected = 'failed' if mode == 'failure' else 'cancelled'
            wait_for(lambda: statuses(history(flow)).get('A') == expected, f'A {expected}', timeout=6)
            (directory / 'release-B').touch()
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 1, (out, err)
    log = history(flow)
    assert log['run']['status'] == ('failed' if mode == 'failure' else 'cancelled')
    assert statuses(log) == {
        'A': 'failed' if mode == 'failure' else 'cancelled',
        'B': 'cancelled' if mode == 'workflow' else 'succeeded',
        'Q': 'succeeded' if mode == 'task' else 'skipped',
        'C': 'skipped', 'D': 'succeeded' if mode == 'task' else 'skipped',
    }
    assert not (directory / 'C.ran').exists()
    assert (directory / 'Q.ran').exists() == (mode == 'task')
    if mode == 'task':
        assert (directory / 'D.result').read_text() == '17'
    else:
        assert not (directory / 'D.result').exists()


@pytest.mark.parametrize('linger', [False, True])
def test_installed_gate_waits_for_child_without_output(installed, linger):
    flow = installed(COMMON + '''
@parallel
def a():
    child = subprocess.Popen([sys.executable, '-c', """
from pathlib import Path
import time
end = time.monotonic() + 10
Path('child.ready').touch()
while not Path('release-child').exists() and time.monotonic() < end:
    time.sleep(.01)
"""], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait('child.ready')
    Path('pids').write_text(str(os.getpid()) + ' ' + str(child.pid))
    wait('return-A')
    return 17
def c(*, item):
    Path('result').write_text(str(item))
tasks = {'A': a, 'C': c}
dag = dict.fromkeys(tasks, ())
inputs = {'C': {'item': 'A'}}
''')
    directory = flow.script.parent
    with running(flow) as proc:
        path = directory / 'pids'
        wait_for(lambda: path.exists() and len(path.read_text().split()) == 2, 'worker and child IDs')
        worker, child = map(int, path.read_text().split())
        group_path = Path('/sys/fs/cgroup') / Path(Path(f'/proc/{child}/cgroup').read_text().strip()[3:]).relative_to('/')
        fd = os.pidfd_open(worker)
        try:
            (directory / 'return-A').touch()
            assert select.select([fd], [], [], 5)[0]
            assert 'populated 1' in (group_path / 'cgroup.events').read_text().splitlines()
            assert not (directory / 'result').exists()
        finally:
            os.close(fd)
        if not linger:
            (directory / 'release-child').touch()
        proc.communicate(timeout=10)
        assert proc.returncode == int(linger)
        assert not group_path.exists()
    log = history(flow)
    assert log['run']['status'] == ('failed' if linger else 'succeeded')
    assert statuses(log) == ({'A': 'failed', 'C': 'skipped'} if linger else {'A': 'succeeded', 'C': 'succeeded'})
    if linger:
        assert not (directory / 'result').exists()
    else:
        assert (directory / 'result').read_text() == '17'


def systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], check=True, capture_output=True, text=True, timeout=5).stdout


@pytest.mark.parametrize('stop', ['timeout', 'supervisor-kill'])
def test_installed_service_boundary_cleans_up_without_supervisor(installed, stop):
    flow = installed(COMMON + '''
@parallel
def a():
    leaf = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path('pids.tmp').write_text(f'{os.getpid()} {leaf.pid}')
    Path('pids.tmp').rename('pids')
    wait('never-release')

tasks = {'A': a}
dag = {'A': set()}
''', '--timeout', '7s' if stop == 'timeout' else '20s')
    canary = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(25)'])
    from contextlib import ExitStack
    try:
        with running(flow) as proc, ExitStack() as stack:
            path = flow.script.parent / 'pids'
            wait_for(path.is_file, 'worker and detached child IDs', timeout=5)
            worker, leaf = map(int, path.read_text().split())
            cgroup = systemctl('show', flow.service, '-p', 'ControlGroup', '--value').strip()
            assert cgroup.endswith('/' + flow.service)
            for pid in (worker, leaf):
                assert Path(f'/proc/{pid}/cgroup').read_text().strip().startswith('0::' + cgroup + '/')
            assert not Path(f'/proc/{canary.pid}/cgroup').read_text().strip().startswith('0::' + cgroup)
            fds = []
            for pid in (worker, leaf):
                fd = os.pidfd_open(pid)
                stack.callback(os.close, fd)
                fds.append(fd)
            if stop == 'supervisor-kill':
                fragment = systemctl('show', flow.service, '-p', 'FragmentPath', '--value').strip()
                assert store().owns_path(flow.service, fragment)
                systemctl('kill', '--kill-whom=main', '--signal=KILL', flow.service)
            proc.communicate(timeout=14)
            assert proc.returncode == 1
            for fd in fds:
                assert select.select([fd], [], [], 5)[0]
            assert canary.poll() is None
            assert systemctl('show', flow.service, '-p', 'Result', '--value').strip() == (
                'timeout' if stop == 'timeout' else 'signal')
            log = history(flow)
            assert log['run']['status'] == 'incomplete' and statuses(log) == {'A': 'incomplete'}
    finally:
        if canary.poll() is None:
            canary.terminate()
        canary.wait(timeout=5)
