"""Small helpers for workflows that keep state between runs. Standard library only."""

import hashlib
import json
import os
from pathlib import Path


def read_json(path: Path, default):
    """The file's JSON, or `default` when the file does not exist yet."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    """Replace the file atomically: readers see the old or the new content, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
