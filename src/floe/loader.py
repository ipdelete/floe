"""Load a workflow definition file into a validated Workflow.

A definition file is plain Python that sets WORKFLOW, tasks, dag and,
optionally, SCHEDULE. Loading executes the file, like importing a module.
"""

import runpy
import sys
from contextlib import contextmanager
from dataclasses import replace
from functools import wraps
from importlib.machinery import all_suffixes
from pathlib import Path

from .model import LoadError, Workflow

__all__ = ["load"]


class _Imports:
    """A workflow's sibling-module cache, active only while its code runs.

    Import state is process-global: loading/running workflows must be serial,
    not concurrent with other threads importing same-named sibling modules.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.modules = {}

    @contextmanager
    def active(self):
        # Include packages (also namespace packages), source and extension modules.
        roots = {name.partition(".")[0] for name in self.modules}
        for entry in self.directory.iterdir():
            if entry.is_dir() and entry.name.isidentifier():
                roots.add(entry.name)
            elif entry.is_file():
                for suffix in all_suffixes():
                    if entry.name.endswith(suffix):
                        roots.add(entry.name[:-len(suffix)])
                        break
        roots.difference_update(sys.builtin_module_names)

        def local_modules():
            return {name: module for name, module in sys.modules.copy().items()
                    if name.partition(".")[0] in roots}

        previous = local_modules()
        previous_path = list(sys.path)
        for name in previous:
            del sys.modules[name]
        sys.modules.update(self.modules)
        sys.path.insert(0, str(self.directory))
        try:
            yield
        finally:
            self.modules = local_modules()
            for name in self.modules:
                del sys.modules[name]
            sys.modules.update(previous)
            sys.path[:] = previous_path

    def wrap(self, task):
        @wraps(task)
        def invoke(*args, **kwargs):
            with self.active():
                return task(*args, **kwargs)
        return invoke


def load(path: str | Path) -> Workflow:
    """Raise LoadError if the file cannot be executed or lacks definitions,
    and ValidationError (listing every problem) if they are invalid.

    Sibling imports are isolated per load and reactivated when tasks run.
    As with runpy, loading and running are not thread-safe.
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise LoadError(f"workflow file not found: {path}")
    imports = _Imports(path.parent)
    try:
        with imports.active():
            namespace = runpy.run_path(str(path), run_name="floe_workflow")
    except Exception as exc:
        raise LoadError(f"{path}: {type(exc).__name__}: {exc}") from exc
    missing = [key for key in ("WORKFLOW", "tasks", "dag") if key not in namespace]
    if missing:
        raise LoadError(f"{path}: missing {', '.join(missing)}")
    workflow = Workflow(
        name=namespace["WORKFLOW"],
        tasks=namespace["tasks"],
        dag=namespace["dag"],
        schedule=namespace.get("SCHEDULE", ()),
        source=path,
    )
    return replace(workflow, tasks={name: imports.wrap(task) for name, task in workflow.tasks.items()})
