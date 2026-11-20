import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from tools.agentic_dev_loop import live_e2e as e2e


ROOT = Path(__file__).resolve().parents[2]


def test_local_target_ignores_checkout_and_environment_aliases(tmp_path, monkeypatch):
    first = e2e.Target.local()
    monkeypatch.chdir(tmp_path)
    for key in ('HOME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_STATE_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    assert e2e.Target.local() == first
    assert first.runtime == Path(f'/run/user/{os.getuid()}')


def invoke(target, checkout, notify):
    try:
        notify.send(e2e.run(target, checkout, Path(sys.executable)))
    except e2e.Blocked as exc:
        notify.send(str(exc))


def launch(target, checkout):
    ctx = multiprocessing.get_context('fork')
    receive, send = ctx.Pipe(duplex=False)
    child = ctx.Process(target=invoke, args=(target, checkout, send))
    child.start()
    send.close()
    return child, receive


def result(child, receive):
    assert receive.poll(8)
    outcome = receive.recv()
    child.join(8)
    assert child.exitcode == 0
    return outcome


def test_manual_and_automated_checkouts_share_lock_through_verification(tmp_path, monkeypatch):
    target = e2e.Target(tmp_path, tmp_path / 'runtime')
    manual, automated = tmp_path / 'manual', tmp_path / 'automated'
    manual.mkdir()
    automated.mkdir()
    ctx = multiprocessing.get_context('fork')
    driver, worker = ctx.Pipe()

    def barrier(phase):
        worker.send(phase)
        assert worker.poll(8)
        assert worker.recv() == 'go'

    def audit(target):
        barrier('audit')

    def suite(target, checkout, python, fd, selection):
        assert checkout == manual
        for phase in ('tests', 'teardown'):
            barrier(phase)
        return 0

    monkeypatch.setattr(e2e, 'verify_clean', audit)
    monkeypatch.setattr(e2e, '_run_suite', suite)
    holder, receipt = launch(target, manual)
    try:
        for phase in ('audit', 'tests', 'teardown', 'audit'):
            assert driver.poll(8)
            assert driver.recv() == phase
            contender, other = launch(target, automated)
            try:
                assert 'busy' in result(contender, other)
            finally:
                if contender.is_alive():
                    contender.kill()
                    contender.join(8)
            driver.send('go')
        assert result(holder, receipt) == 0
    finally:
        if holder.is_alive():
            holder.kill()
            holder.join(8)


def test_crash_blocks_reuse_until_group_and_manager_are_clean(tmp_path, monkeypatch):
    target = e2e.Target(tmp_path, tmp_path / 'runtime')
    ctx = multiprocessing.get_context('fork')
    ready = ctx.Event()
    group = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)

    def suite(target, checkout, python, fd, selection):
        e2e._write_dirty(target, e2e.RunGroup.current(group.pid))
        ready.set()
        signal.pause()

    monkeypatch.setattr(e2e, 'verify_clean', lambda _: None)
    monkeypatch.setattr(e2e, '_run_suite', suite)
    holder, _ = launch(target, ROOT)
    try:
        assert ready.wait(8)
        holder.kill()
        holder.join(8)
        assert holder.exitcode == -signal.SIGKILL
        with pytest.raises(e2e.Blocked, match='recover'):
            e2e.run(target, ROOT, Path(sys.executable))
        with pytest.raises(e2e.Blocked, match='process group'):
            e2e.recover(target)
        group.kill()
        group.wait(timeout=8)
        for reason in ('query failed', 'unit present', 'file present', 'enable link present', 'stamp present'):
            def failed_audit(_):
                raise e2e.Blocked(reason)
            monkeypatch.setattr(e2e, 'verify_clean', failed_audit)
            with pytest.raises(e2e.Blocked, match=reason):
                e2e.recover(target)
            with pytest.raises(e2e.Blocked, match='recover'):
                e2e.run(target, ROOT, Path(sys.executable))
        monkeypatch.setattr(e2e, 'verify_clean', lambda _: None)
        e2e.recover(target)
        monkeypatch.setattr(e2e, '_run_suite', lambda *args: 0)
        assert e2e.run(target, ROOT, Path(sys.executable)) == 0
    finally:
        if holder.is_alive():
            holder.kill()
            holder.join(8)
        if group.poll() is None:
            group.kill()
            group.wait(timeout=8)


@pytest.mark.parametrize('storage_failure', [False, True])
def test_suite_exec_is_gated_on_persisting_its_process_group(tmp_path, monkeypatch, storage_failure):
    target = e2e.Target(tmp_path, tmp_path / 'runtime')
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    (checkout / 'pytest.ini').write_text('[pytest]\nmarkers = systemd_e2e\n')
    (checkout / 'conftest.py').write_text('''
def pytest_addoption(parser):
    parser.addoption('--systemd-e2e', action='store_true')
''')
    witness = tmp_path / 'entered'
    (checkout / 'test_fixture.py').write_text(f'''
import json
import os
from pathlib import Path
import pytest

@pytest.mark.systemd_e2e
def test_held_lease():
    fd = int(os.environ['FLOE_E2E_LEASE_FD'])
    assert os.path.samestat(os.fstat(fd), Path({str(target.lock)!r}).stat())
    pid = json.loads(Path({str(target.dirty)!r}).read_text())['pid']
    assert pid == os.getpid() == os.getpgrp() == os.getsid(0)
    Path({str(witness)!r}).touch()
''')
    save = e2e._write_dirty

    def write(target, group):
        if storage_failure and group.pid is not None:
            raise OSError('injected dirty write failure')
        save(target, group)

    monkeypatch.setattr(e2e, '_write_dirty', write)
    monkeypatch.setattr(e2e, 'verify_clean', lambda _: None)
    if storage_failure:
        with pytest.raises(OSError, match='injected'):
            e2e.run(target, checkout, Path(sys.executable))
        assert target.dirty.exists()
        assert not witness.exists()
    else:
        assert e2e.run(target, checkout, Path(sys.executable)) == 0
        assert witness.exists()
        assert not target.dirty.exists()


def test_failed_suite_requires_recovery_even_when_postflight_finds_no_residue(tmp_path, monkeypatch):
    target = e2e.Target(tmp_path, tmp_path / 'runtime')
    audits = []
    monkeypatch.setattr(e2e, 'verify_clean', audits.append)
    monkeypatch.setattr(e2e, '_run_suite', lambda *args: 1)
    assert e2e.run(target, ROOT, Path(sys.executable)) == 1
    assert audits == [target, target]
    with pytest.raises(e2e.Blocked, match='recover'):
        e2e.run(target, ROOT, Path(sys.executable))
    e2e.recover(target)
    assert not target.dirty.exists()


@pytest.mark.parametrize('residue', ['loaded', 'queued', 'query-failed', 'foreign-file', 'mask',
                                     'enable-link', 'alias-link', 'stamp', None])
def test_audit_never_mutates_unrelated_or_unknown_units(tmp_path, monkeypatch, residue):
    target = e2e.Target(tmp_path, tmp_path / 'runtime')
    units = target.home / '.config/systemd/user'
    units.mkdir(parents=True)
    unrelated = units / 'unrelated.service'
    unrelated.write_text('leave me alone')
    remaining = None
    if residue in ('foreign-file', 'mask'):
        remaining = units / 'floe-e2e-foreign.service'
        if residue == 'mask':
            remaining.symlink_to('/dev/null')
        else:
            remaining.write_text('unowned fixture')
    elif residue == 'enable-link':
        directory = units / 'timers.target.wants'
        directory.mkdir()
        remaining = directory / 'floe-e2e-orphan.timer'
        remaining.symlink_to('../floe-e2e-orphan.timer')
    elif residue == 'alias-link':
        remaining = units / 'other-name.service'
        remaining.symlink_to('floe-e2e-orphan.service')
    elif residue == 'stamp':
        directory = target.home / '.local/share/systemd/timers'
        directory.mkdir(parents=True)
        remaining = directory / 'stamp-floe-e2e-orphan.timer'
        remaining.touch()

    traces = []

    def query(command, *, env, capture_output, text, timeout, check):
        traces.append(command)
        assert env['XDG_RUNTIME_DIR'] == str(target.runtime)
        assert check and capture_output and text and timeout > 0
        match command[:3]:
            case ['/usr/bin/busctl', '--user', '--json=short']:
                assert command[3] == 'get-property' and command[-1] == 'UnitPath'
                output = f'{{"type":"as","data":["{units}"]}}'
            case ['/usr/bin/systemctl', '--user', 'list-units']:
                assert command[-1] == 'floe-e2e-*'
                if residue == 'query-failed':
                    raise subprocess.CalledProcessError(1, command)
                output = 'floe-e2e-unknown.service loaded active running\n' if residue == 'loaded' else ''
            case ['/usr/bin/systemctl', '--user', 'list-jobs']:
                assert command[-1] == 'floe-e2e-*'
                output = '42 floe-e2e-unknown.service stop waiting\n' if residue == 'queued' else ''
            case ['/usr/bin/systemctl', '--user', 'show-environment']:
                output = ''
            case _:
                pytest.fail(f'unexpected command: {command}')
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(e2e.subprocess, 'run', query)
    if residue:
        with pytest.raises(e2e.Blocked):
            e2e.verify_clean(target)
    else:
        e2e.verify_clean(target)
    assert traces
    assert unrelated.read_text() == 'leave me alone'
    if remaining:
        assert remaining.exists() or remaining.is_symlink()


@pytest.mark.parametrize('module', ['test_systemd_e2e', 'test_parallel_systemd_e2e', 'test_managed_systemd_e2e'])
def test_raw_opt_in_refuses_all_live_modules_before_manager_access(tmp_path, module):
    trace = tmp_path / 'trace'
    for name in ('systemctl', 'systemd-run', 'busctl', 'journalctl', 'floe'):
        executable = tmp_path / name
        executable.write_text(f'#!/bin/sh\necho {name} >> "{trace}"\nexit 1\n')
        executable.chmod(0o700)
    args = [sys.executable, '-m', 'pytest', '-xq', f'tests/{module}.py']
    env = {**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}', 'FLOE_E2E_LEASE_FD': '0'}
    # Failing before collection also prevents any module-scoped sweep or setup.
    raw = subprocess.run([*args, '--systemd-e2e', '--collect-only'], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=20)
    assert raw.returncode == 4, raw.stdout + raw.stderr
    assert 'requires a live E2E lease' in raw.stderr
    assert not trace.exists()
