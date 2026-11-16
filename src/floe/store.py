"""Explicit, ownership-protected file operations for generated Floe units.

This module never calls systemctl. Preflight a whole deployment before writing
its individual files; atomic replacement protects each file, not the pair.
"""

import os
import re
import tempfile
from pathlib import Path


class NotOwnedError(PermissionError):
    """Refused to overwrite or delete a foreign, linked, or masked unit."""


def _canonical(path: Path) -> Path:
    """Normalize a path whose final component may not exist (or be a symlink)."""
    path = Path(os.path.normpath(path))
    return Path(os.path.realpath(path.parent)) / path.name


class UnitStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()

    def _file(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(service|timer)", name):
            raise ValueError(f"invalid unit filename: {name!r}")
        return self.path / name

    def is_owned(self, name: str) -> bool:
        file = self._file(name)
        if file.is_symlink() or not file.is_file():
            return False
        # Inspect only our marker, not arbitrary systemd directive semantics.
        section = owner = None
        for line in file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif section == "X-Floe":
                key, sep, value = line.partition("=")
                if sep and key.strip() == "Owner":
                    owner = value.strip()
        return owner == "floe"

    def preflight(self, name: str, *, adopt: bool = False) -> None:
        file = self._file(name)
        if file.is_symlink() or (file.exists() and not file.is_file()):
            raise NotOwnedError(f"{file} is not a regular file (masked or linked?)")
        if file.exists() and not adopt and not self.is_owned(name):
            raise NotOwnedError(f"{file} is not managed by floe (use --adopt)")

    def write(self, name: str, text: str, *, adopt: bool = False) -> bool:
        """Atomically replace a unit if content changed; report whether it did."""
        self.preflight(name, adopt=adopt)
        file = self._file(name)
        if file.exists() and file.read_text() == text:
            return False
        self._atomic_write(file, text)
        return True

    def remove(self, name: str) -> bool:
        """Remove an owned unit and enable links pointing to it; report whether
        the unit file existed. A missing file is OK: dangling enable links left
        by an interrupted removal are still cleaned up."""
        file = self._file(name)
        existed = file.exists() or file.is_symlink()
        if existed:
            self.preflight(name)
            file.unlink()
        self.remove_links(name)
        return existed

    def remove_links(self, name: str) -> list[Path]:
        """Remove enable links to this directory's unit, even if dangling."""
        file = self._file(name)
        if not self.path.is_dir():
            return []
        wanted = _canonical(file)
        removed = []
        for directory in self.path.iterdir():
            if (directory.is_dir() and not directory.is_symlink()
                    and directory.suffix in (".wants", ".requires", ".upholds")):
                link = directory / name
                if link.is_symlink() and _canonical(link.parent / os.readlink(link)) == wanted:
                    link.unlink()
                    removed.append(link)
        return removed

    def owns_path(self, name: str, path: str) -> bool:
        """Whether a path reported by systemd is this store's file for name."""
        return bool(path) and _canonical(Path(path)) == _canonical(self._file(name))

    @staticmethod
    def _atomic_write(file: Path, text: str) -> None:
        file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=f".{file.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
