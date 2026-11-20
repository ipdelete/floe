"""Leased, supervised Pi delivery probe; no production admission path."""

import json
import os
import re
import stat
import subprocess
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
            for name in ("config.json", "tick-token", "worker-token", "copilot.json", "models-store.json"):
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
            # These must be independently provisioned credentials, not aliases.
            if (directory / "tick-token").read_bytes().strip() == (directory / "worker-token").read_bytes().strip():
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
                "--ro-bind", str(self.directory / f"{role}-token"), "/input/github",
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
            "-p", "UMask=0077", "-p", "LimitCORE=0", "-p", "NoNewPrivileges=yes",
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
        receipt = json.loads((output / "outcome.json").read_text())
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


def live_inputs():
    return SmokeInputs.load(Target.local().home / ".local/state/floe-pi-smoke-inputs")