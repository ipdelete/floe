"""Opt-in validation of the implementation, separate from installed-unit E2E.

One fresh transient delegated service; never adopt or clean up another unit.
Normal tests skip this module's test without querying the manager.
"""

import asyncio
import ctypes
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time
import uuid

import pytest

import floe
from floe.results import RunResults
from floe.scheduler import TaskSchedule
from floe.worker import observe_managed_worker

FLOW = Path(__file__).parent.resolve() / 'fixtures' / 'managed_gate_flow.py'
MOUNT = Path('/sys/fs/cgroup')


def current_path():
    value, = [s[3:] for s in Path('/proc/self/cgroup').read_text().splitlines() if s.startswith('0::')]
    path = Path(value)
    assert path.is_absolute() and '..' not in path.parts
    return MOUNT / path.relative_to('/')


async def until(check):
    async with asyncio.timeout(6):
        while not check():
            await asyncio.sleep(.01)


async def case(root, *, linger):
    workflow = floe.load(FLOW)
    results = RunResults(workflow.inputs)
    schedule = TaskSchedule(workflow, 2)

    async def drive():
        while (task := schedule.next_task()) is not None:
            try:
                observed = await observe_managed_worker(
                    workflow, task, results, 'gate-test', lambda *_: None,
                    timeout=16, drain_timeout=1 if linger else 6, cleanup_timeout=4)
            except TimeoutError:
                if not linger or task != 'A':
                    raise
                schedule.finish(task, 'failed')
            else:
                assert observed.error is None and observed.returncode == 0, observed
                results.complete(task, json.loads(observed.value) if observed.value is not None else None)
                schedule.finish(task, 'succeeded')

    driver = asyncio.create_task(drive())
    try:
        await until(Path('members').is_file)
        worker, leaf, location = json.loads(Path('members').read_text())
        group = MOUNT / Path(location.strip().removeprefix('0::')).relative_to('/')
        assert group.parent == root and re.fullmatch(r'floe-task-[a-f0-9]{32}', group.name)
        assert set(map(int, (group / 'cgroup.procs').read_text().split())) == {worker, leaf}
        assert os.getsid(leaf) == leaf
        fd = os.pidfd_open(worker)
        try:
            Path('worker.release').touch()
            await until(lambda: select.select([fd], [], [], 0)[0])
        finally:
            os.close(fd)
        assert set(map(int, (group / 'cgroup.procs').read_text().split())) == {leaf}
        assert 'populated 1' in (group / 'cgroup.events').read_text().splitlines()
        assert not driver.done() and 'C' not in schedule.outcomes and not Path('received').exists()
        if not linger:
            Path('leaf.release').touch()
        await driver
        assert group.name not in {p.name for p in root.iterdir()}
        # Subreaper status is distinct evidence from cgroup removal.
        async with asyncio.timeout(4):
            while True:
                pid, status = os.waitpid(leaf, os.WNOHANG)
                if pid:
                    assert os.waitstatus_to_exitcode(status) == (-9 if linger else 0)
                    break
                await asyncio.sleep(.01)
        if linger:
            assert schedule.outcomes == {'A': 'failed', 'C': 'skipped'} and not Path('received').exists()
        else:
            assert schedule.run_outcome == 'succeeded' and Path('received').read_text() == '17'
    finally:
        Path('worker.release').touch()
        Path('leaf.release').touch()
        if not driver.done():
            driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)


def service_probe(unit):
    root = current_path()
    if not re.fullmatch(r'floe-e2e-managed-[0-9a-f]{32}\.service', unit) or root.name != unit:
        raise RuntimeError(f'refusing probe outside its generated service: {root}')
    Path('service-root').write_text(str(root))
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:  # Linux PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'cannot subreap fixture child')
    workdir = Path.cwd()
    for name, linger in [('release', False), ('timeout', True)]:
        directory = workdir / name
        directory.mkdir()
        os.chdir(directory)
        try:
            asyncio.run(case(root, linger=linger))
        finally:
            os.chdir(workdir)
    print('both managed gate cases passed')


def unit_state(unit):
    result = subprocess.run(['systemctl', '--user', 'show', '-p', 'LoadState,ActiveState', unit],
                            capture_output=True, check=True, text=True, timeout=5)
    return dict(line.split('=', 1) for line in result.stdout.splitlines())


@pytest.mark.systemd_e2e
def test_managed_worker_gate_in_one_delegated_service(tmp_path):
    unit = f'floe-e2e-managed-{uuid.uuid4().hex}.service'
    absent = {'LoadState': 'not-found', 'ActiveState': 'inactive'}
    assert unit_state(unit) == absent
    try:
        result = subprocess.run([
            'systemd-run', '--user', '--wait', '--pipe', '--collect', '--expand-environment=no',
            f'--unit={unit}', f'--working-directory={tmp_path}', '-p', 'Type=exec', '-p', 'Delegate=yes',
            '-p', 'RuntimeMaxSec=55s', '-p', 'TimeoutStopSec=5s', '-p', 'KillMode=control-group',
            sys.executable, '-B', str(Path(__file__).resolve()), unit,
        ], capture_output=True, text=True, timeout=75)
        assert result.returncode == 0, (unit, result.stderr, result.stdout)
        assert 'both managed gate cases passed' in result.stdout
        assert (tmp_path / 'service-root').is_file(), f'missing cgroup location evidence for {unit}'
    finally:
        end = time.monotonic() + 6
        while unit_state(unit) != absent:
            if time.monotonic() > end:
                pytest.fail(f'collection unconfirmed for {unit}; inspect this unit only')
            time.sleep(.05)
        marker = tmp_path / 'service-root'
        if marker.is_file():
            path = Path(marker.read_text())
            assert path.name == unit and path.name not in {p.name for p in path.parent.iterdir()}


if __name__ == '__main__':
    service_probe(sys.argv[1])
