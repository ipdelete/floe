"""CRUD layer: a directory of unit files as a MutableMapping.

Only touches files. It never runs systemctl; after changes, systemd needs
``systemctl --user daemon-reload`` before it notices them.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator, MutableMapping
from pathlib import Path

from . import ini
from .models import MODELS, FloeSection, UnitFile

HEADER = "Managed by {owner}. Manual edits will be overwritten."


class NotOwnedError(PermissionError):
    """Refused to overwrite or delete a unit file not marked as ours."""


class UnitDir(MutableMapping[str, UnitFile]):
    """
    units = UnitDir("~/.config/systemd/user", owner="floe")
    units["x.service"] = ServiceFile(...)   # create/update (atomic, owned)
    units["x.service"]                      # read
    del units["x.service"]                  # delete (+ dangling *.wants links)
    list(units)                             # owned regular unit files

    With owner=None the mapping is unrestricted: it reads and lists every unit
    file and overwrites without a marker. Use that for reading foreign units.
    """

    def __init__(self, path: str | os.PathLike[str], owner: str | None = "floe"):
        self.path = Path(path).expanduser()
        self.owner = owner

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self.path)!r}, owner={self.owner!r})"

    # ----- mapping protocol ----------------------------------------------
    def __getitem__(self, name: str) -> UnitFile:
        file = self._file(name)
        if not self._is_unit_file(file):
            raise KeyError(name)
        raw = ini.parse(file.read_text())
        if not self._owns(raw):
            raise KeyError(name)
        return MODELS[file.suffix].from_raw(raw)

    def __setitem__(self, name: str, unit: UnitFile) -> None:
        self.put(name, unit)

    def __delitem__(self, name: str) -> None:
        file = self._file(name)
        if not self._is_unit_file(file):
            raise KeyError(name)
        if not self._owns(ini.parse(file.read_text())):
            raise NotOwnedError(f"{file} is not owned by {self.owner!r}")
        file.unlink()
        self._remove_links_to(file)

    def __iter__(self) -> Iterator[str]:
        if not self.path.is_dir():
            return
        for file in sorted(self.path.iterdir()):
            if file.name in self:
                yield file.name

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, name: object) -> bool:
        # Avoid parsing the whole file through __getitem__ just for a test,
        # but still honour ownership.
        try:
            file = self._file(name)  # type: ignore[arg-type]
        except KeyError:
            return False
        return self._is_unit_file(file) and self._owns(ini.parse(file.read_text()))

    # ----- explicit operations -------------------------------------------
    def put(self, name: str, unit: UnitFile, *, force: bool = False) -> bool:
        """Write ``unit``. Returns False if the file already had this content.

        ``force=True`` takes over an existing file that isn't ours."""
        file = self._file(name)
        if not isinstance(unit, MODELS[file.suffix]):
            raise TypeError(f"{name} needs a {MODELS[file.suffix].__name__}, got {type(unit).__name__}")
        if file.is_symlink() or (file.exists() and not file.is_file()):
            raise NotOwnedError(f"{file} is not a regular file (masked or linked?)")
        if file.exists() and not force and not self._owns(ini.parse(file.read_text())):
            raise NotOwnedError(f"{file} exists and is not owned by {self.owner!r}")

        text = self.render(unit)
        if file.is_file() and file.read_text() == text:
            return False
        self._atomic_write(file, text)
        return True

    def render(self, unit: UnitFile) -> str:
        """The exact text put() writes for ``unit`` (ownership marker included)."""
        header = None
        if self.owner is not None:
            marker = unit.x_floe or FloeSection(owner=self.owner)
            unit = unit.model_copy(update={"x_floe": marker.model_copy(update={"owner": self.owner})})
            header = HEADER.format(owner=self.owner)
        return ini.serialize(unit.to_raw(), header=header)

    # ----- internals -----------------------------------------------------
    def _file(self, name: str) -> Path:
        if not isinstance(name, str) or "/" in name or name.startswith("."):
            raise KeyError(name)
        file = self.path / name
        if file.suffix not in MODELS:
            raise KeyError(name)
        stem = file.stem
        if "@" in stem and not stem.endswith("@"):
            # "dag@daily.service" is an instance: it has no file of its own.
            raise KeyError(name)
        return file

    @staticmethod
    def _is_unit_file(file: Path) -> bool:
        return file.suffix in MODELS and file.is_file() and not file.is_symlink()

    def _owns(self, raw: ini.RawUnit) -> bool:
        if self.owner is None:
            return True
        return ("Owner", self.owner) in raw.get("X-Floe", [])

    def _remove_links_to(self, file: Path) -> None:
        """Remove symlinks left by `systemctl enable` (e.g. timers.target.wants/x.timer)."""
        for d in self.path.iterdir():
            if d.is_dir() and not d.is_symlink() and d.suffix in (".wants", ".requires", ".upholds"):
                link = d / file.name
                if link.is_symlink():
                    link.unlink()

    @staticmethod
    def _atomic_write(file: Path, text: str) -> None:
        file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=f".{file.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
