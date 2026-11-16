"""Build ExecStart= style command lines from an argv list.

Rules from systemd.syntax(7) "Quoting" and systemd.service(5) "Command lines":
- items containing whitespace or quotes are wrapped in double quotes, with
  C-style escapes for backslash and double quote;
- an argument consisting solely of ";" must be written "\\;";
- "%" is a specifier prefix: literal "%" is "%%" (systemd.unit(5));
- "$" triggers variable expansion: literal "$" is "$$".
"""

from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def exec_line(argv: list[str]) -> str:
    if not argv:
        raise ValueError("empty command")
    return " ".join(_quote(a) for a in argv)


def _quote(arg: str) -> str:
    if _CONTROL.search(arg):
        raise ValueError(f"control characters are not allowed in commands: {arg!r}")
    if arg == ";":
        return "\\;"
    s = arg.replace("\\", "\\\\").replace('"', '\\"')
    if not arg or any(c.isspace() for c in arg) or "'" in arg or '"' in arg:
        s = f'"{s}"'
    return s.replace("%", "%%").replace("$", "$$")
