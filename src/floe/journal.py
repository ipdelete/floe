"""Thin wrapper over journalctl. The only place floe reads the journal.

Like floe.systemd, every call is the exact command you'd type, and the class
can be replaced with a fake in tests.
"""

import json
import re
import subprocess
import tempfile
from collections.abc import Iterator

from .model import SystemdError, ValidationError

__all__ = ["FIELDS", "Journal"]

# Everything history needs; journalctl always adds __REALTIME_TIMESTAMP and __CURSOR.
FIELDS = (
    "MESSAGE", "PRIORITY", "_SYSTEMD_INVOCATION_ID", "USER_INVOCATION_ID", "MESSAGE_ID",
    "UNIT_RESULT", "JOB_RESULT", "JOB_TYPE", "_TRANSPORT", "SYSLOG_IDENTIFIER", "EXIT_CODE", "EXIT_STATUS",
)


class Journal:
    def __init__(self, user: bool = True):
        self.base = ["journalctl", "--user"] if user else ["journalctl"]

    def records(self, unit: str, *, since: str | None = None, reverse: bool = False,
                invocations: tuple[str, ...] | None = None) -> Iterator[dict]:
        """Stream a unit's journal records as dicts, oldest first unless reverse.

        Records are read lazily: stopping iteration early stops journalctl.
        `since` takes any journalctl --since value ("24 hours ago", "today", ...).
        `invocations` selects both application and manager records for those IDs.
        """
        if invocations is not None:
            if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value)
                   for value in invocations):
                raise ValidationError(["invocation IDs must be 32 lowercase hex digits"])
            if not invocations:
                return
        cmd = [*self.base, f"--unit={unit}", "--output=json", "--no-pager",
               f"--output-fields={','.join(FIELDS)}"]
        if since is not None:
            cmd.append(f"--since={since}")
        if reverse:
            cmd.append("--reverse")
        if invocations is not None:
            cmd.extend(f"_SYSTEMD_INVOCATION_ID={value}" for value in invocations)
            cmd.append("+")  # either the process's ID or the manager's ID
            cmd.extend(f"USER_INVOCATION_ID={value}" for value in invocations)
        with tempfile.TemporaryFile("w+") as stderr:  # a file cannot fill up and block
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr, text=True)
            finished = False
            try:
                for line in proc.stdout:
                    if line.strip():
                        yield json.loads(line)
                finished = True
            finally:
                if not finished:
                    proc.kill()
                proc.stdout.close()
                proc.wait()
            if proc.returncode:
                stderr.seek(0)
                raise SystemdError(cmd, proc.returncode, stderr.read())
