"""Trusted Linux local-user-manager entry point (deploy outside candidate worktrees)."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import uuid


_PREFIX = 'floe-e2e-'
_LEASE_FD = 'FLOE_E2E_LEASE_FD'
_BOOT_ID = Path('/proc/sys/kernel/random/boot_id')


class Blocked(Exception):
    """The entry point has no evidence it can safely proceed."""


@dataclass(frozen=True)
class Target:
    home: Path
    runtime: Path

    @classmethod
    def local(cls):
        uid = os.getuid()
        if uid == 0 or uid != os.geteuid():
            raise Blocked('live E2E requires a non-root local user session')
        # No checkout, worker, alias, or caller-supplied bus/state environment
        # participates in identity. The only supported endpoint is the host's
        # canonical local user manager for this UID.
        return cls(Path(pwd.getpwuid(uid).pw_dir).resolve(), Path(f'/run/user/{uid}'))

    @property
    def state(self) -> Path:
        return self.home / '.local/state/floe-live-e2e'

    @property
    def lock(self) -> Path:
        return self.state / 'lock'

    @property
    def dirty(self) -> Path:
        return self.state / 'dirty.json'

    def environment(self, bin_dir: Path = Path('/usr/bin')) -> dict[str, str]:
        return {
            'HOME': str(self.home), 'XDG_RUNTIME_DIR': str(self.runtime),
            'DBUS_SESSION_BUS_ADDRESS': f'unix:path={self.runtime}/bus',
            'PATH': f'/usr/bin:/bin:{bin_dir}', 'LC_ALL': 'C.UTF-8',
            'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
        }


@dataclass(frozen=True)
class RunGroup:
    boot: str
    pid: int | None

    @classmethod
    def current(cls, pid: int | None):
        return cls(_BOOT_ID.read_text().strip(), pid)

    def verify_gone(self) -> None:
        if self.pid is not None and self.boot == _BOOT_ID.read_text().strip():
            try:
                os.killpg(self.pid, 0)  # Query only; do not signal a possibly reused PGID.
            except ProcessLookupError:
                return
            raise Blocked(f'prior E2E process group {self.pid} is still present or reused')


def _check_private(info: os.stat_result, kind) -> None:
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Blocked('live E2E state and lock must be private and user-owned')


@contextmanager
def _locked(target: Target):
    target.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_private(target.state.lstat(), stat.S_ISDIR)
    fd = os.open(target.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        _check_private(info, stat.S_ISREG)
        if info.st_nlink != 1:
            raise Blocked('live E2E lock must have a single link')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Blocked('live E2E manager is busy') from exc
        yield fd
    finally:
        # Never explicitly unlock the shared file description: a surviving
        # pytest child keeps the lease if the supervisor exits unexpectedly.
        os.close(fd)


def _sync_state(target: Target) -> None:
    fd = os.open(target.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_dirty(target: Target, group: RunGroup) -> None:
    with tempfile.NamedTemporaryFile(mode='w', dir=target.state, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump({'boot': group.boot, 'pid': group.pid}, stream)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(target.dirty)
            _sync_state(target)
        finally:
            temporary.unlink(missing_ok=True)


def _read_dirty(target: Target) -> RunGroup | None:
    try:
        info = target.dirty.lstat()
    except FileNotFoundError:
        return None
    _check_private(info, stat.S_ISREG)
    try:
        payload = json.loads(target.dirty.read_text())
        boot, pid = payload['boot'], payload['pid']
        if str(uuid.UUID(boot)) != boot or (pid is not None and (type(pid) is not int or pid <= 1)):
            raise ValueError('invalid process group')
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Blocked('invalid dirty E2E record; inspect state before reuse') from exc
    return RunGroup(boot, pid)


def require_lease() -> None:
    """The common pytest opt-in guard; deliberately makes no manager calls."""
    try:
        target = Target.local()
        fd = int(os.environ[_LEASE_FD])
        lock = os.fstat(fd)
        _check_private(lock, stat.S_ISREG)
        if not os.path.samestat(lock, target.lock.lstat()):
            raise Blocked('wrong lock descriptor')
        pid = os.getpid()
        if os.getsid(0) != pid or os.getpgrp() != pid or _read_dirty(target) != RunGroup.current(pid):
            raise Blocked('lease does not belong to this suite')
        # An independently opened description must be excluded, but the
        # inherited open description must own the lock.
        probe = os.open(target.lock, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                raise Blocked('lease is not held')
        finally:
            os.close(probe)
        os.set_inheritable(fd, False)
        del os.environ[_LEASE_FD]
    except (Blocked, KeyError, ValueError, OSError) as exc:
        raise Blocked('raw --systemd-e2e requires a live E2E lease; use the trusted live_e2e.py entry point') from exc


def _query(target: Target, tool: str, *args: str) -> str:
    try:
        return subprocess.run(
            [f'/usr/bin/{tool}', '--user', *args], env=target.environment(),
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Blocked(f'live E2E query failed: {tool} {args[0]}') from exc


def _unit_paths(target: Target) -> tuple[Path, ...]:
    reply = _query(target, 'busctl', '--json=short', 'get-property',
                   'org.freedesktop.systemd1', '/org/freedesktop/systemd1',
                   'org.freedesktop.systemd1.Manager', 'UnitPath')
    try:
        value = json.loads(reply)
        paths = value['data']
        if value['type'] != 'as' or not isinstance(paths, list) or not paths:
            raise ValueError('expected manager unit paths')
        if not all(isinstance(path, str) and Path(path).is_absolute() for path in paths):
            raise ValueError('expected absolute manager unit paths')
        return tuple(Path(path) for path in paths)
    except (TypeError, KeyError, ValueError) as exc:
        raise Blocked('cannot establish the live manager unit paths') from exc


def _entries(directory: Path) -> tuple[Path, ...]:
    try:
        return tuple(directory.iterdir())
    except FileNotFoundError:
        # A dangling symlink or an inaccessible path is not a missing directory.
        if directory.is_symlink():
            raise Blocked(f'cannot inspect unit or state directory {directory}') from None
        return ()


def _e2e_entry(path: Path) -> bool:
    if path.name.startswith(_PREFIX):
        return True
    return path.is_symlink() and path.readlink().name.startswith(_PREFIX)


def verify_clean(target: Target) -> None:
    """Read-only initial sweep, final verification, and recovery audit."""
    for query in ('list-units', 'list-jobs'):
        if _query(target, 'systemctl', query, '--all', '--plain', '--no-legend', '--no-pager',
                  f'{_PREFIX}*').strip():
            raise Blocked(f'{query} reports live E2E residue; inspect generated units before reuse')

    # Support the maintainer's ordinary (default XDG layout) local manager.
    # Refuse visible overrides rather than guessing which timer state to inspect.
    expected = {'HOME': str(target.home), 'XDG_DATA_HOME': str(target.home / '.local/share'),
                'XDG_CONFIG_HOME': str(target.home / '.config'), 'XDG_RUNTIME_DIR': str(target.runtime)}
    for line in _query(target, 'systemctl', 'show-environment').splitlines():
        key, sep, value = line.partition('=')
        if not sep or (key in expected and value != expected[key]):
            raise Blocked('cannot confirm the live manager uses the default XDG layout')

    unit_home = target.home / '.config/systemd/user'
    paths = _unit_paths(target)
    if unit_home not in paths:
        raise Blocked('live manager unit path does not include the default user unit directory')
    for path in paths:
        for entry in _entries(path):
            if _e2e_entry(entry):
                raise Blocked(f'live E2E unit file, alias or drop-in remains: {entry}')
            if entry.name.endswith(('.wants', '.requires', '.upholds')):
                for link in _entries(entry):
                    if _e2e_entry(link):
                        raise Blocked(f'live E2E enable link remains: {link}')
    timers = target.home / '.local/share/systemd/timers'
    for stamp in _entries(timers):
        if stamp.name.startswith(f'stamp-{_PREFIX}'):
            raise Blocked(f'live E2E timer stamp remains: {stamp}')


def _run_suite(target: Target, checkout: Path, python: Path, fd: int, selection: str | None) -> int:
    read, write = os.pipe()
    try:
        with subprocess.Popen(
            [str(python), '-I', str(Path(__file__).resolve()), '_suite', str(read),
             *([selection] if selection is not None else [])],
            cwd=checkout, env={**target.environment(python.parent), _LEASE_FD: str(fd)},
            pass_fds=(read, fd), start_new_session=True,
        ) as child:
            # The child cannot start pytest until its PGID is durably recorded.
            # If we die before that, EOF denies entry. Always close the write end
            # before waiting, including on an error persisting the process group.
            try:
                _write_dirty(target, RunGroup.current(child.pid))
                os.write(write, b'+')
            finally:
                os.close(write)
                write = None
            try:
                return child.wait(timeout=900)
            except subprocess.TimeoutExpired as exc:
                child.kill()  # This is our pytest child, not an inferred process group.
                raise Blocked('live E2E suite timed out; inspect residue before recovery') from exc
    finally:
        os.close(read)
        if write is not None:
            os.close(write)


def _suite(gate: int, selection: list[str]) -> None:
    try:
        if os.read(gate, 1) != b'+':
            raise Blocked('live E2E supervisor did not release its startup gate')
    finally:
        os.close(gate)
    args = [sys.executable, '-I', '-m', 'pytest', '-q', '-m', 'systemd_e2e', '--systemd-e2e']
    if selection:
        args += ['-k', selection[0]]
    os.execv(sys.executable, args)


def run(target: Target, checkout: Path, python: Path, selection: str | None = None) -> int:
    with _locked(target) as fd:
        if _read_dirty(target) is not None:
            raise Blocked('previous live E2E run is dirty; use recover after inspecting residue')
        _write_dirty(target, RunGroup.current(None))
        verify_clean(target)
        status = _run_suite(target, checkout, python, fd, selection)
        # A nonzero pytest status may itself report a teardown failure. Even
        # if the immediate audit passes, require explicit reconciliation.
        _verify_and_clear(target, clear=status == 0)
        return status


def _verify_and_clear(target: Target, *, clear: bool) -> None:
    group = _read_dirty(target)
    if group is not None:
        group.verify_gone()
    verify_clean(target)
    if clear and group is not None:
        target.dirty.unlink()
        _sync_state(target)


def recover(target: Target) -> None:
    with _locked(target):
        _verify_and_clear(target, clear=True)


def main(argv: list[str]) -> int:
    try:
        if len(argv) in (2, 3) and argv[0] == '_suite':
            _suite(int(argv[1]), argv[2:])
            return 1
        parser = argparse.ArgumentParser(description=__doc__)
        commands = parser.add_subparsers(dest='operation', required=True)
        suite = commands.add_parser('run', help='run the full authorized live suite')
        suite.add_argument('--checkout', required=True, type=Path)
        suite.add_argument('--python', required=True, type=Path, help='candidate virtualenv interpreter')
        suite.add_argument('-k', help='pytest expression to run a subset of the live suite')
        commands.add_parser('recover', help='verify cleanup; do not mutate the manager or its units')
        args = parser.parse_args(argv)
        target = Target.local()
        if args.operation == 'recover':
            recover(target)
            return 0
        status = run(target, args.checkout.absolute(), args.python.absolute(), args.k)
        if status:
            print('live E2E returned nonzero; inspect the failure and use recover before reuse', file=sys.stderr)
        return 128 - status if status < 0 else status
    except (Blocked, OSError) as exc:
        print(f'live E2E blocked: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
