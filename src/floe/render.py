"""Render Floe's two unit files, not arbitrary systemd configuration."""

import re
from pathlib import Path

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def exec_line(argv: list[str]) -> str:
    """Quote literal argv for systemd, whose command syntax is not a shell's."""
    if not argv:
        raise ValueError("empty command")
    return " ".join(_quote(arg) for arg in argv)


def _quote(arg: str) -> str:
    if _CONTROL.search(arg):
        raise ValueError(f"control characters are not allowed in commands: {arg!r}")
    if arg == ";":
        return "\\;"
    value = arg.replace("\\", "\\\\").replace('"', '\\"')
    if not arg or any(c.isspace() for c in arg) or "'" in arg or '"' in arg:
        value = f'"{value}"'
    return value.replace("%", "%%").replace("$", "$$")


def check_value(value: str, where: str) -> str:
    """Reject values that could inject directives or change on serialization."""
    if (
        not isinstance(value, str)
        or not value
        or _CONTROL.search(value)
        or value != value.strip()
        or value.endswith("\\")
    ):
        raise ValueError(f"invalid {where}: {value!r}")
    return value


def literal(value: str) -> str:
    """Non-command directives expand % specifiers, but not $ variables."""
    return check_value(value, "literal value").replace("%", "%%")


def render_service(workflow: str, script: Path, python: str, timeout: str) -> str:
    if not python.startswith("/"):
        raise ValueError("python must be an absolute executable path")
    check_value(python, "python")
    check_value(str(script), "script")
    return (
        "# Managed by floe. Manual edits will be overwritten.\n\n"
        f"[Unit]\nDescription=Floe workflow {workflow}\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart={exec_line([python, str(script), 'run'])}\n"
        f"WorkingDirectory={literal(str(script.parent))}\n"
        f"TimeoutStartSec={literal(timeout)}\n"
        "KillMode=control-group\nStandardOutput=journal\nStandardError=journal\n"
        f"SyslogIdentifier={workflow}\n\n"
        f"[X-Floe]\nOwner=floe\nWorkflow={workflow}\n"
    )


def render_timer(workflow: str, schedule: tuple[str, ...], persistent: bool) -> str:
    triggers = "".join(f"OnCalendar={value}\n" for value in schedule)
    return (
        "# Managed by floe. Manual edits will be overwritten.\n\n"
        f"[Unit]\nDescription=Schedule floe workflow {workflow}\n\n"
        f"[Timer]\n{triggers}Persistent={'yes' if persistent else 'no'}\n"
        f"Unit={workflow}.service\n\n"
        "[Install]\nWantedBy=timers.target\n\n"
        f"[X-Floe]\nOwner=floe\nWorkflow={workflow}\n"
    )
