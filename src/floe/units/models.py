"""Typed layer: pydantic models mirroring unit file sections.

Field names are snake_case; their PascalCase aliases are the systemd keys.
Unknown keys are kept (``extra="allow"``) so reading and writing again never
drops a directive these models don't declare.

Assignment semantics follow the man pages (systemd 259):

- systemd.syntax(7): an empty assignment resets a list *only where the
  setting's description says so*. Elsewhere values are kept verbatim.
- Settings documented as "space-separated lists" are split on read and written
  back as one line.
- systemd.timer(5): an empty assignment to any of OnCalendar=/On*Sec= resets
  *all* of them.
- Single-valued settings: the last assignment wins.

Values are stored as systemd literals: ``%h`` is kept verbatim, a literal
percent sign must be written ``%%`` (systemd.unit(5) "Specifiers"), and in
command lines a literal ``$`` must be written ``$$`` (systemd.service(5)
"Command lines"; not checked here, since ``$FOO`` is legitimate).
"""

from __future__ import annotations

import re
import typing
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic.alias_generators import to_pascal

from .ini import RawUnit

Value = str | list[str]

# Specifiers listed in systemd.unit(5), plus "%%".
_SPECIFIERS = set("aAbBCdDEfgGhHiIjJlLmMnNopPqsStTuUvVwWyY%")
_PERCENT = re.compile(r"%(.?)", re.S)  # consumes left to right, so "%%F" is "%%" + "F"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def _check_value(v: str, where: str) -> None:
    """Reject values that would not read back as written (systemd.syntax(7))."""
    if _CONTROL.search(v):
        raise ValueError(f"{where}: control characters (e.g. newline) are not allowed: {v!r}")
    if v != v.strip():
        raise ValueError(f"{where}: leading/trailing whitespace is stripped by systemd: {v!r}")
    if v.endswith("\\"):
        raise ValueError(f"{where}: a trailing backslash continues the line: {v!r}")
    if any(m not in _SPECIFIERS for m in _PERCENT.findall(v)):
        raise ValueError(f"{where}: unknown specifier in {v!r}; write %% for a literal %")


class Section(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="allow", alias_generator=to_pascal, populate_by_name=True
    )
    # Keys documented as space-separated lists: split on read, one line on write.
    space_lists: ClassVar[frozenset[str]] = frozenset()
    # Groups of keys where an empty assignment to any member resets all members.
    reset_groups: ClassVar[tuple[frozenset[str], ...]] = ()

    @model_validator(mode="after")
    def _validate_values(self):
        resettable = set().union(*self.reset_groups)
        for key, value in self.items():
            if not _KEY.fullmatch(key):
                raise ValueError(f"invalid key {key!r}")
            for v in value if isinstance(value, list) else [value]:
                if not isinstance(v, str):
                    continue
                if key in resettable and v == "":
                    raise ValueError(f"{key}: an empty item would reset the list; omit it instead")
                if key in self.space_lists and (v == "" or any(c.isspace() for c in v)):
                    raise ValueError(f"{key}: items are space-separated; one item per list entry: {v!r}")
                _check_value(v, key)
        return self

    def items(self) -> list[tuple[str, Any]]:
        """(systemd key, value) for declared fields, then unknown keys."""
        pairs = [(f.alias or n, getattr(self, n)) for n, f in type(self).model_fields.items()]
        return pairs + list((self.__pydantic_extra__ or {}).items())

    @classmethod
    def keys(cls) -> set[str]:
        return {f.alias or n for n, f in cls.model_fields.items()}

    @classmethod
    def list_keys(cls) -> set[str]:
        return {f.alias or n for n, f in cls.model_fields.items() if typing.get_origin(f.annotation) is list}

    # ----- read: [(key, value)] -> field dict ------------------------------
    @classmethod
    def collect(cls, pairs: list[tuple[str, str]]) -> dict[str, Value]:
        known, lists = cls.keys(), cls.list_keys()
        out: dict[str, Value] = {}
        for key, value in pairs:
            group = next((g for g in cls.reset_groups if key in g), None)
            if group is not None and value == "":
                for k in group:
                    out.pop(k, None)
            elif key in cls.space_lists:
                out[key] = [*out.get(key, []), *value.split()]
            elif key in lists:
                out[key] = [*out.get(key, []), value]
            elif key in known:
                out[key] = value  # last assignment wins
            elif key in out:  # unknown repeated key: keep every value verbatim
                prev = out[key]
                out[key] = [*prev, value] if isinstance(prev, list) else [prev, value]
            else:
                out[key] = value
        return out

    # ----- write: field values -> [(key, value)] ---------------------------
    def pairs(self) -> list[tuple[str, str]]:
        out = []
        for key, value in self.items():
            if value is None or value == []:
                continue
            if key in self.space_lists:
                out.append((key, " ".join(value)))
            else:
                out += [(key, _fmt(v)) for v in (value if isinstance(value, list) else [value])]
        return out


# Unit dependency keys: "space-separated list ... may be specified more than once"
# (systemd.unit(5)); no reset documented for them.
class UnitSection(Section):
    space_lists = frozenset({"Documentation", "After", "Before", "Wants", "Requires"})
    reset_groups = (frozenset({"Documentation"}),)  # documented reset

    description: str | None = None
    documentation: list[str] = []
    after: list[str] = []
    before: list[str] = []
    wants: list[str] = []
    requires: list[str] = []


class InstallSection(Section):
    space_lists = frozenset({"WantedBy", "RequiredBy", "Alias"})

    wanted_by: list[str] = []
    required_by: list[str] = []
    alias: list[str] = []


class ServiceSection(Section):
    # systemd.service(5)/systemd.exec(5) document a reset for these two only.
    reset_groups = (frozenset({"ExecStart"}), frozenset({"Environment"}))

    type: Literal["simple", "exec", "forking", "oneshot", "dbus", "notify", "notify-reload", "idle"] | None = None
    exec_start: list[str] = []
    exec_start_pre: list[str] = []
    exec_start_post: list[str] = []
    exec_stop: list[str] = []
    working_directory: str | None = None
    environment: list[str] = []
    timeout_start_sec: str | None = None
    timeout_stop_sec: str | None = None
    kill_mode: Literal["control-group", "mixed", "process", "none"] | None = None
    standard_output: str | None = None
    standard_error: str | None = None
    syslog_identifier: str | None = None

    @model_validator(mode="after")
    def _one_exec_start_unless_oneshot(self):
        # "Unless Type= is oneshot, exactly one command must be given." Type
        # defaults to simple when ExecStart= is set, so None counts as non-oneshot.
        if self.type != "oneshot" and len(self.exec_start) > 1:
            raise ValueError("multiple ExecStart= lines require Type=oneshot")
        return self


TIMER_TRIGGERS = frozenset(
    {"OnCalendar", "OnActiveSec", "OnBootSec", "OnStartupSec", "OnUnitActiveSec", "OnUnitInactiveSec"}
)


class TimerSection(Section):
    # systemd.timer(5): an empty assignment to any trigger resets all of them.
    reset_groups = (TIMER_TRIGGERS,)

    on_calendar: list[str] = []
    on_active_sec: list[str] = []
    on_boot_sec: list[str] = []
    on_startup_sec: list[str] = []
    on_unit_active_sec: list[str] = []
    on_unit_inactive_sec: list[str] = []
    persistent: bool | None = None
    randomized_delay_sec: str | None = None
    accuracy_sec: str | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def _unit_not_timer(self):
        if self.unit is not None and self.unit.endswith(".timer"):
            raise ValueError("Unit= must name a unit whose suffix is not .timer")
        return self


class FloeSection(Section):
    """[X-Floe]: ignored by systemd (X- prefix), used to mark ownership."""

    owner: str
    workflow: str | None = None


class UnitFile(BaseModel):
    """A whole unit file. Subclasses declare one field per section."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    suffix: ClassVar[str]
    _section_names: ClassVar[dict[str, str]] = {"x_floe": "X-Floe"}

    unit: UnitSection = UnitSection()
    install: InstallSection | None = None
    x_floe: FloeSection | None = None
    # Sections no model declares, kept verbatim: name -> [(key, value)]
    other_sections: dict[str, list[tuple[str, str]]] = {}

    @classmethod
    def section_name(cls, field: str) -> str:
        return cls._section_names.get(field) or to_pascal(field)

    @classmethod
    def section_fields(cls) -> list[str]:
        """Canonical order: [Unit], type-specific sections, [Install], [X-Floe]."""
        base = set(UnitFile.model_fields)
        own = [n for n in cls.model_fields if n not in base]
        return ["unit", *own, "install", "x_floe"]

    @classmethod
    def from_raw(cls, raw: RawUnit) -> "UnitFile":
        fields = {cls.section_name(n): n for n in cls.section_fields()}
        data: dict[str, Any] = {"other_sections": {}}
        for name, pairs in raw.items():
            if name not in fields:
                data["other_sections"][name] = list(pairs)
                continue
            data[fields[name]] = _section_type(cls, fields[name]).collect(pairs)
        return cls.model_validate(data)

    def to_raw(self) -> RawUnit:
        raw: RawUnit = {}
        for n in self.section_fields():
            section = getattr(self, n)
            if section is None:
                continue
            pairs = section.pairs()
            if pairs or n != "unit":
                raw[self.section_name(n)] = pairs
        raw.update({k: list(v) for k, v in self.other_sections.items()})
        return raw


class ServiceFile(UnitFile):
    suffix: ClassVar[str] = ".service"
    service: ServiceSection


class TimerFile(UnitFile):
    suffix: ClassVar[str] = ".timer"
    timer: TimerSection


MODELS: dict[str, type[UnitFile]] = {m.suffix: m for m in (ServiceFile, TimerFile)}


def _section_type(cls: type[UnitFile], field: str) -> type[Section]:
    ann = cls.model_fields[field].annotation
    return next(a for a in (ann, *typing.get_args(ann)) if isinstance(a, type) and issubclass(a, Section))


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"  # syntax(7): 1/yes/true/on, 0/no/false/off
    return str(value)
