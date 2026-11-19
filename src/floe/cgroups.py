"""Private Linux containment for cooperative tasks in a delegated cgroup v2 service.

Only create fresh children of the supervisor's own cgroup. Never adopt a group,
use caller-supplied paths, or recursively remove children. Not a sandbox.
"""

import asyncio
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import uuid

from .model import FloeError

_MOUNT = Path('/sys/fs/cgroup')
_DIR = os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW


def _current_root() -> Path:
    paths = [line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::')]
    if len(paths) != 1:
        raise FloeError('no unified cgroup membership')
    path = Path(paths[0])
    if path.anchor != '/' or path == Path('/') or '..' in path.parts:
        raise FloeError('expected a delegated non-root cgroup')
    return _MOUNT / path.relative_to('/')


def _on_cgroup2(fd: int) -> bool:
    # Match the *opened directory*, not just a path that happens to lie under
    # /sys/fs/cgroup. A writable lookalike or submount must not pass preflight.
    info = Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines()
    ids = [line.split()[1:] for line in info if line.startswith('mnt_id:')]
    if len(ids) != 1 or len(ids[0]) != 1:
        return False
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, _, right = line.partition(' - ')
        if left.split()[:1] == ids[0]:
            return right.split()[:1] == ['cgroup2']
    return False


@dataclass(frozen=True)
class _OwnedGroup:
    path: Path
    parent_fd: int
    fd: int

    def _verify(self):
        entry = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
        if not os.path.samestat(entry, os.fstat(self.fd)):
            raise FloeError(f'owned task group was replaced: {self.path}')

    def open_control(self, name, flags):
        self._verify()
        return os.open(name, flags | os.O_NOFOLLOW, dir_fd=self.fd)

    def populated(self) -> bool:
        with os.fdopen(self.open_control('cgroup.events', os.O_RDONLY)) as events:
            values = [fields[1:] for line in events if (fields := line.split()) and fields[0] == 'populated']
        if values not in ([['0']], [['1']]):
            raise FloeError(f'unknown population of task group {self.path}')
        return values == [['1']]

    async def wait_empty(self):
        while self.populated():
            await asyncio.sleep(.02)

    def kill(self):
        fd = self.open_control('cgroup.kill', os.O_WRONLY)
        try:
            if os.write(fd, b'1') != 1:
                raise FloeError(f'incomplete kill request for {self.path}')
        finally:
            os.close(fd)

    def remove_empty(self):
        if self.populated():
            raise FloeError(f'task group still populated: {self.path}')
        self._verify()
        os.rmdir(self.path.name, dir_fd=self.parent_fd)


@contextmanager
def _new_group():
    path = _current_root() / ('floe-task-' + uuid.uuid4().hex)
    with ExitStack() as stack:
        parent = os.open(path.parent, _DIR)
        stack.callback(os.close, parent)
        # Fail before creating anything unless this is an owned v2 delegation
        # containing this process. No fallback to a parent or sibling group.
        if not _on_cgroup2(parent):
            raise FloeError(f'not a verified cgroup v2 mount: {path.parent}')
        if os.fstat(parent).st_uid != os.geteuid():
            raise FloeError(f'current cgroup is not owned by supervisor: {path.parent}')
        for control in ('cgroup.controllers', 'cgroup.procs'):
            with os.fdopen(os.open(control, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)) as stream:
                if control == 'cgroup.procs' and not any(line.strip() == str(os.getpid()) for line in stream):
                    raise FloeError(f'supervisor not in delegation {path.parent}')
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
        try:
            group_fd = os.open(path.name, _DIR, dir_fd=parent)
        except OSError as exc:
            raise FloeError(f'cannot verify newly created group {path}; inspect before cleanup') from exc
        stack.callback(os.close, group_fd)
        yield _OwnedGroup(path, parent, group_fd)


@asynccontextmanager
async def managed_group(cleanup_timeout):
    """Keep the directory as a recovery marker if owned cleanup cannot be verified."""
    with _new_group() as group:
        try:
            if group.populated():
                raise FloeError(f'new task group unexpectedly populated: {group.path}')
            # Missing/forbidden kill is a launch blocker, not a discovery at timeout.
            os.close(group.open_control('cgroup.kill', os.O_WRONLY))
        except BaseException as setup_error:
            try:
                group.remove_empty()  # never kill unexpected occupants during setup
            except (OSError, FloeError) as exc:
                raise FloeError(f'{setup_error!r}; setup cleanup unconfirmed for {group.path}: {exc!r}') from exc
            raise
        try:
            yield group
            group.remove_empty()  # even a transient removal failure discards the result
        except BaseException as original:
            try:
                # Target only the pinned group; even an unknown population is not
                # permission to claim success. Recheck after the kill request.
                group.kill()
                async with asyncio.timeout(cleanup_timeout):
                    await group.wait_empty()
                group.remove_empty()
            except asyncio.CancelledError as exc:
                exc.add_note(f'cleanup interrupted for owned task group {group.path}')
                raise
            except (OSError, FloeError) as exc:
                raise FloeError(f'{original!r}; cleanup unconfirmed for owned task group {group.path}: {exc!r}') from exc
            raise
