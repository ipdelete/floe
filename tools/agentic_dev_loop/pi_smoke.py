"""Leased, supervised Pi delivery probe; no production admission path."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import pi_probe
from .installed_fixture import FixtureManager, REQUEST
from .launcher import Reservation, WorkerState, WorkerUnavailable
from .live_e2e import Target


@dataclass(frozen=True)
class SmokeInputs:
    directory: Path
    tick_login: str
    worker_login: str
    node: Path
    release: Path

    @classmethod
    def load(cls, directory):
        """Never fall back to an ambient login, provider store, or executable."""
        try:
            if not pi_probe.private(directory, stat.S_ISDIR, 0o700):
                raise ValueError
            for name in ("config.json", "github-token", "copilot.json", "models-store.json"):
                path = directory / name
                if not pi_probe.private(path, stat.S_ISREG, 0o600) or path.stat().st_nlink != 1:
                    raise ValueError
            config = json.loads((directory / "config.json").read_text())
            if set(config) != {"tick_login", "worker_login", "node", "release"}:
                raise ValueError
            for field in ("tick_login", "worker_login"):
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,38}", config[field]):
                    raise ValueError
            node, release = (Path(config[field]).resolve(strict=True) for field in ("node", "release"))
            if (not all(Path(config[field]).is_absolute() for field in ("node", "release"))
                    or not node.is_file() or not (release / "node_modules/.bin/pi").is_file()):
                raise ValueError
            if not (directory / "github-token").read_text().strip():
                raise ValueError
            auth = json.loads((directory / "copilot.json").read_text())
            if not isinstance(auth, dict) or set(auth) != {"github-copilot"}:
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError):
            raise WorkerUnavailable("required private smoke provisioning unavailable") from None
        return cls(directory, config["tick_login"], config["worker_login"], node, release)

    def sandbox(self, role, output, *, worktree=None, run_id="", session_id=""):
        if role not in ("tick", "worker"):
            raise ValueError("unknown smoke role")
        if role == "worker" and worktree is None:
            raise ValueError("worker requires an isolated worktree")
        args = ["/usr/bin/bwrap", "--clearenv", "--unshare-user", "--disable-userns", "--unshare-pid",
                "--unshare-ipc", "--die-with-parent", "--new-session", "--ro-bind", "/usr", "/usr",
                "--symlink", "usr/lib", "/lib", "--symlink", "usr/bin", "/bin", "--proc", "/proc",
                "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/sandbox-home", "--dir", "/work",
                "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
                "--ro-bind", "/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/certs/ca-certificates.crt",
                "--ro-bind", str(Path(pi_probe.__file__).resolve()), "/probe.py",
                "--ro-bind", str(self.directory / "github-token"), "/input/github",
                "--bind", str(output), "/output"]
        if Path("/lib64").is_symlink():
            args += ["--symlink", os.readlink("/lib64"), "/lib64"]
        for name, value in pi_probe.ENV.items():
            args += ["--setenv", name, value]
        if role == "worker":
            args += ["--ro-bind", str(self.node), "/node", "--ro-bind", str(self.release), "/pi",
                     "--ro-bind", str(self.directory / "copilot.json"), "/input/copilot.json",
                     "--ro-bind", str(self.directory / "models-store.json"), "/input/models-store.json"]
            args += ["--ro-bind", str(worktree), "/work"]
        identity = self.worker_login if role == "worker" else self.tick_login
        return args + ["--remount-ro", "/", "--setenv", "PWD", "/work", "--chdir", "/work", "--",
                       "/usr/bin/python3.14", "-I", "/probe.py", role, identity, run_id, session_id]


class SmokeManager(FixtureManager):
    def __init__(self, state, inputs, storage):
        super().__init__(Path("/usr/bin/python3.14"), state)
        self.inputs = inputs
        self.storage = storage
        self.worktree = state / "worktree"

    def io(self, reservation):
        # Validate canonical UUID before using it as a path component.
        self.unit_name(reservation.run_id)
        return self.storage / reservation.run_id

    def dispatch(self, role, reservation, output):
        # Only the leased pytest cases call this adapter. The pytest entry
        # consumes the inherited descriptor before collecting any tests.
        unit = self.unit_name(reservation.run_id)
        if not self._absent(self._show(unit)):
            raise WorkerUnavailable("not a new smoke unit")
        argv = [
            "/usr/bin/systemd-run", "--user", "--no-ask-password", "--collect", "--service-type=exec",
            "--expand-environment=no", f"--unit={unit}", f"--description={self._description(reservation)}",
            "-p", "RuntimeMaxSec=80s", "-p", "TimeoutStopSec=3s", "-p", "KillMode=control-group",
            "-p", "ExitType=cgroup",
            "-p", "StandardInput=null", "-p", "StandardOutput=null", "-p", "StandardError=null",
            "-p", "UMask=0077", "-p", "LimitCORE=0", "-p", "LimitFSIZE=1M", "-p", "NoNewPrivileges=yes",
            "-p", "RestrictAddressFamilies=AF_INET AF_INET6 AF_NETLINK",
            "-p", "SystemCallFilter=~@keyring io_uring_setup", "-p", "SystemCallErrorNumber=EPERM",
            "-p", "SystemCallArchitectures=native",
            "/usr/bin/env", "-i", *self.inputs.sandbox(role, output, worktree=self.worktree,
                                                        run_id=reservation.run_id, session_id=reservation.session_id),
        ]
        try:
            subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            raise WorkerUnavailable("smoke service dispatch not confirmed") from None

    def tick(self):
        # The trusted supervisor hands off only after the tick's read-only
        # check exits; it does not give the credential-bearing tick a bus.
        reservation = Reservation(REQUEST.key, str(uuid4()), "", str(uuid4()))
        output = self.state / "tick-probe"
        output.mkdir(mode=0o700)
        try:
            self.dispatch("tick", reservation, output)
            deadline = time.monotonic() + 35
            while not self._absent(self._show(self.unit_name(reservation.run_id))):
                if time.monotonic() >= deadline:
                    raise WorkerUnavailable("tick smoke did not collect")
                time.sleep(0.05)
            if (outcome := read_outcome(output, reservation)) != "ok":
                raise WorkerUnavailable(outcome or "tick smoke has no verified outcome")
        finally:
            if self._owns(self._show(self.unit_name(reservation.run_id)), reservation):
                self.stop(reservation)

    def start(self, reservation, request):
        if request != REQUEST:
            raise WorkerUnavailable("not the fixed smoke request")
        output = self.io(reservation)
        output.mkdir(mode=0o700)
        (output / "sessions").mkdir(mode=0o700)
        self.dispatch("worker", reservation, output)

    def inspect(self, reservation):
        unit = self._show(self.unit_name(reservation.run_id))
        if not self._absent(unit):
            if not self._owns(unit, reservation):
                return WorkerState.FOREIGN
            # Wait through the inactive/failed transition until systemd has
            # actually collected the transient service.
            return WorkerState.RUNNING
        outcome = read_outcome(self.io(reservation), reservation)
        if outcome == "ok":
            if not pi_probe.verify_session(self.io(reservation) / "sessions", reservation.session_id):
                raise WorkerUnavailable("native session not verified")
            return WorkerState.SUCCEEDED
        if outcome is not None:
            return WorkerState.FAILED
        return super().inspect(reservation)


def read_outcome(output, reservation):
    try:
        fd = os.open(output / "outcome.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError
            data = stream.read(1025)
            if len(data) > 1024:
                raise ValueError
            receipt = json.loads(data)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise WorkerUnavailable("smoke outcome unreadable") from None
    if (not isinstance(receipt, dict) or set(receipt) != {"run_id", "session_id", "outcome"}
            or not isinstance(outcome := receipt["outcome"], str)
            or (outcome != "ok" and outcome not in pi_probe.FAILURES)):
        raise WorkerUnavailable("smoke outcome invalid")
    if (receipt["run_id"], receipt["session_id"]) != (reservation.run_id, reservation.session_id):
        raise WorkerUnavailable("smoke outcome does not match the reservation")
    return outcome


def input_directory(home):
    return home / ".local/state/floe-pi-smoke-inputs"


def live_inputs():
    return SmokeInputs.load(input_directory(Target.local().home))


def _gh_value(args, env):
    result = subprocess.run(["/usr/bin/gh", *args], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=10)
    value = result.stdout.decode().strip()
    if not value:
        raise ValueError
    return value


def provision(home: Path, node: Path | None) -> None:
    """Stage only the smoke inputs from the operator's existing logins."""
    destination = input_directory(home)
    if os.path.lexists(destination):
        raise WorkerUnavailable("private smoke provisioning already exists")
    try:
        if node is None or not node.is_absolute():
            raise ValueError
        node = node.resolve(strict=True)
        agent = home / ".pi/agent"
        release = (agent / "install/releases/1.5.2").resolve(strict=True)
        provider, model_id = pi_probe.MODEL.split("/", 1)
        auth = json.loads((agent / "auth.json").read_text())[provider]
        if not isinstance(auth, dict) or not auth:
            raise ValueError
        catalog = json.loads((agent / "models-store.json").read_text())[provider]
        [model] = [entry for entry in catalog["models"] if entry["id"] == model_id and entry["provider"] == provider]
        metadata = {key: catalog[key] for key in ("etag", "lastModified", "checkedAt") if key in catalog}
        env = {**pi_probe.ENV, "HOME": str(home), "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
        token = _gh_value(["auth", "token", "--hostname", "github.com"], env)
        login = _gh_value(["api", "--hostname", "github.com", "--method", "GET", "--jq", ".login", "user"],
                          {**env, "GH_TOKEN": token})
        files = {
            "config.json": json.dumps({"tick_login": login, "worker_login": login, "node": str(node),
                                       "release": str(release)}),
            "github-token": token + "\n",
            "copilot.json": json.dumps({provider: auth}),
            "models-store.json": json.dumps({provider: {**metadata, "models": [model]}}),
        }
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".floe-pi-smoke-", dir=destination.parent) as stage:
            staged = Path(stage) / "inputs"
            staged.mkdir(mode=0o700)
            for name, value in files.items():
                fd = os.open(staged / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(value)
            SmokeInputs.load(staged)
            staged.rename(destination)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise WorkerUnavailable("could not provision private smoke inputs from existing logins") from None


def main(argv):
    if argv != ["provision"]:
        print("usage: python -m tools.agentic_dev_loop.pi_smoke provision", file=sys.stderr)
        return 2
    try:
        executable = shutil.which("node")
        provision(Target.local().home, Path(executable) if executable else None)
    except WorkerUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Private Pi smoke inputs provisioned.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))