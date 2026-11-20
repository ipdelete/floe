"""Rules read from the source, for things that break when they slip."""

import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).parents[2]
OURS = {"floe", "floe_cli", "floe_web"}
UIS = {"floe_cli", "floe_web"}


def python_files(*packages):
    return sorted(path for package in packages for path in (ROOT / "src" / package).rglob("*.py"))


def imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.lineno, node.module


def distribution(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def test_third_party_imports_are_declared_dependencies():
    # The dev environment has more installed than a clean install or a deployed service.
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = {distribution(re.match(r"[A-Za-z0-9._-]+", requirement).group())
                for requirement in pyproject["project"]["dependencies"]}
    installed = packages_distributions()
    undeclared = []
    for path in python_files(*OURS):
        for line, module in imports(ast.parse(path.read_text())):
            top = module.split(".")[0]
            if top in sys.stdlib_module_names or top in OURS:
                continue
            if not {distribution(name) for name in installed.get(top, [top])} & declared:
                undeclared.append(f"{path.relative_to(ROOT)}:{line}: {module}")

    assert undeclared == []


def test_library_does_not_import_a_ui_print_or_exit():
    # Printing corrupts `floe runs --json` and other output scripts parse; exiting kills `floe web`.
    problems = []
    for path in python_files("floe"):
        tree = ast.parse(path.read_text())
        where = path.relative_to(ROOT)
        problems += [f"{where}:{line}: imports {module}"
                     for line, module in imports(tree) if module.split(".")[0] in UIS]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                    "print", "exit", "quit"):
                problems.append(f"{where}:{node.lineno}: {node.func.id}()")
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and (node.value.id, node.attr) in (("sys", "exit"), ("sys", "stdout"),
                                                       ("sys", "stderr"), ("os", "_exit"))):
                problems.append(f"{where}:{node.lineno}: {node.value.id}.{node.attr}")

    assert problems == []
