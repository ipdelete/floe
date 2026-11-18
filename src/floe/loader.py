"""Load a workflow definition file into a validated Workflow.

A definition file is plain Python that sets WORKFLOW, tasks, dag and,
optionally, SCHEDULE. Loading executes the file, like importing a module.
"""

import runpy
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import wraps
from importlib.machinery import all_suffixes
from pathlib import Path
from types import ModuleType

from .model import LoadError, ValidationError, Workflow, _is_async_task

__all__ = ["load"]


def _sibling_modules(roots: set[str]) -> dict[str, ModuleType]:
    return {name: module for name, module in sys.modules.copy().items()
            if name.partition(".")[0] in roots}


class _WorkflowImports:
    """A workflow's sibling-module cache, active only while its code runs.

    Import state is process-global: loading/running workflows must be serial,
    not concurrent with other threads importing same-named sibling modules.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.modules: dict[str, ModuleType] = {}

    def _module_roots(self) -> set[str]:
        """Include cached modules, packages (also namespaces), and extensions."""
        roots = {name.partition(".")[0] for name in self.modules}
        for entry in self.directory.iterdir():
            if entry.is_dir() and entry.name.isidentifier():
                roots.add(entry.name)
            elif entry.is_file():
                for suffix in all_suffixes():
                    if entry.name.endswith(suffix):
                        roots.add(entry.name[:-len(suffix)])
                        break
        return roots.difference(sys.builtin_module_names)

    @contextmanager
    def active(self) -> Iterator[None]:
        roots = self._module_roots()
        previous = _sibling_modules(roots)
        previous_path = list(sys.path)
        for name in previous:
            del sys.modules[name]
        sys.modules.update(self.modules)
        sys.path.insert(0, str(self.directory))
        try:
            yield
        finally:
            self.modules = _sibling_modules(roots)
            for name in self.modules:
                del sys.modules[name]
            sys.modules.update(previous)
            sys.path[:] = previous_path

    def wrap(self, task: Callable[..., object]) -> Callable[..., object]:
        # Leave unsupported async callables visible to definition validation.
        if _is_async_task(task):
            return task

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
    imports = _WorkflowImports(path.parent)
    try:
        with imports.active():
            namespace = runpy.run_path(str(path), run_name="floe_workflow")
            missing = [key for key in ("WORKFLOW", "tasks", "dag") if key not in namespace]
            if missing:
                raise LoadError(f"{path}: missing {', '.join(missing)}")
            tasks = namespace["tasks"]
            if isinstance(tasks, Mapping):
                # Leave invalid values intact so Workflow can aggregate their errors.
                tasks = {name: imports.wrap(task) if callable(task) else task for name, task in tasks.items()}
            # Lazy mappings/prerequisites/schedules execute definition code too.
            return Workflow(
                name=namespace["WORKFLOW"],
                tasks=tasks,
                dag=namespace["dag"],
                schedule=namespace.get("SCHEDULE", ()),
                source=path,
                retries=namespace.get("retries", {}),
            )
    except (LoadError, ValidationError):
        raise
    except Exception as exc:
        raise LoadError(f"{path}: {type(exc).__name__}: {exc}") from exc
