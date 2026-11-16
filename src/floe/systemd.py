"""Thin wrapper over systemctl. The only place floe talks to the service manager.

Uses subprocess so it stays stdlib-only and easy to debug: every call is the
exact command you'd type.
"""

from __future__ import annotations

import subprocess

from .model import SystemdError

__all__ = ["Systemd", "SystemdError"]


class Systemd:
    def __init__(self, user: bool = True):
        self.base = ["systemctl", "--user"] if user else ["systemctl"]

    def _run(self, *args: str, ok: tuple[int, ...] = (0,)) -> str:
        cmd = [*self.base, *args]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode not in ok:
            raise SystemdError(cmd, r.returncode, r.stderr)
        return r.stdout

    def daemon_reload(self) -> None:
        self._run("daemon-reload")

    def enable(self, unit: str, now: bool = False) -> None:
        self._run("enable", *(["--now"] if now else []), unit)

    def disable(self, unit: str, now: bool = False) -> None:
        self._run("disable", *(["--now"] if now else []), unit)

    def start(self, unit: str, wait: bool = True) -> None:
        """For Type=oneshot, waits until the service has finished unless wait=False."""
        self._run("start", *([] if wait else ["--no-block"]), unit)

    def stop(self, unit: str, missing_ok: bool = False) -> None:
        """With missing_ok, a unit that is not loaded (exit 5) counts as stopped."""
        self._run("stop", unit, ok=(0, 5) if missing_ok else (0,))

    def reset_failed(self, unit: str) -> None:
        """Forget a unit's failed state, so systemd can unload a removed unit."""
        self._run("reset-failed", unit)

    def show(self, unit: str, *props: str) -> dict[str, str]:
        out = self._run("show", unit, f"--property={','.join(props)}")
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
