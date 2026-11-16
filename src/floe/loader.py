"""Load a workflow definition file into a validated Workflow.

A definition file is plain Python that sets WORKFLOW, tasks, dag and,
optionally, SCHEDULE. Loading executes the file, like importing a module.
"""

import runpy
import sys
from pathlib import Path

from .model import LoadError, Workflow

__all__ = ["load"]


def load(path: str | Path) -> Workflow:
    """Raise LoadError if the file cannot be executed or lacks definitions,
    and ValidationError (listing every problem) if they are invalid."""
    path = Path(path).resolve()
    if not path.is_file():
        raise LoadError(f"workflow file not found: {path}")
    # Like running a script: sibling modules of the definition are importable.
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    try:
        namespace = runpy.run_path(str(path), run_name="floe_workflow")
    except Exception as exc:
        raise LoadError(f"{path}: {type(exc).__name__}: {exc}") from exc
    missing = [key for key in ("WORKFLOW", "tasks", "dag") if key not in namespace]
    if missing:
        raise LoadError(f"{path}: missing {', '.join(missing)}")
    return Workflow(
        name=namespace["WORKFLOW"],
        tasks=namespace["tasks"],
        dag=namespace["dag"],
        schedule=namespace.get("SCHEDULE", ()),
        source=path,
    )
