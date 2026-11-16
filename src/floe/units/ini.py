"""Format layer: systemd unit file text <-> RawUnit.

Knows the syntax only (sections, key=value, comments, line continuations,
repeated keys). Knows nothing about which keys exist or what they mean.
"""

from __future__ import annotations

Section = list[tuple[str, str]]
RawUnit = dict[str, Section]  # ordered: section name -> ordered (key, value) pairs


class UnitSyntaxError(ValueError):
    pass


def parse(text: str) -> RawUnit:
    raw: RawUnit = {}
    current: Section | None = None
    pending = ""
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not pending and (not stripped or stripped[0] in "#;"):
            continue
        if pending and stripped and stripped[0] in "#;":
            continue  # comments inside a continuation are ignored by systemd
        if stripped.endswith("\\"):
            # syntax(7): "the backslash is replaced by a space character"
            pending += stripped[:-1] + " "
            continue
        stripped, pending = pending + stripped, ""
        if stripped.startswith("[") and stripped.endswith("]"):
            current = raw.setdefault(stripped[1:-1], [])
            continue
        if current is None:
            raise UnitSyntaxError(f"line {lineno}: assignment outside of a section")
        key, sep, value = stripped.partition("=")
        if not sep or not key.strip():
            raise UnitSyntaxError(f"line {lineno}: expected key=value, got {line!r}")
        current.append((key.strip(), value.strip()))
    if pending:
        raise UnitSyntaxError("file ends inside a line continuation")
    return raw


def serialize(raw: RawUnit, header: str | None = None) -> str:
    out: list[str] = []
    if header:
        out += [f"# {h}" if h else "#" for h in header.splitlines()]
    for name, pairs in raw.items():
        if out:
            out.append("")
        out.append(f"[{name}]")
        out += [f"{k}={v}" for k, v in pairs]
    return "\n".join(out) + "\n"
