"""Enforce the architecture: the floe library holds the logic and never talks to
users; UIs (floe_cli and floe_web) use only floe's public API; everything is
standard library only, so systemd can run it with the system Python."""

import ast
import sys
from pathlib import Path

import pytest

import floe

SRC = Path(__file__).parents[1] / "src"
LIBRARY = sorted((SRC / "floe").rglob("*.py"))
UIS = sorted(path for package in ("floe_cli", "floe_web") for path in (SRC / package).rglob("*.py"))
OURS = {"floe", "floe_cli", "floe_web"}


def tree(path):
    return ast.parse(path.read_text(), filename=str(path))


def imported_modules(module):
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.module


def rel(path):
    return path.relative_to(SRC)


@pytest.mark.parametrize("path", LIBRARY + UIS, ids=rel)
def test_standard_library_only(path):
    third_party = {name for name in imported_modules(tree(path))
                   if name.split(".")[0] not in sys.stdlib_module_names | OURS}
    assert not third_party


@pytest.mark.parametrize("path", LIBRARY, ids=rel)
def test_library_never_does_user_io(path):
    """No printing, prompting, exiting, or argument parsing; never imports a UI."""
    problems = []
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                "print", "input", "exit", "quit"):
            problems.append(f"line {node.lineno}: {node.func.id}()")
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys"
                and node.attr in ("argv", "exit", "stdout", "stderr", "stdin")):
            problems.append(f"line {node.lineno}: sys.{node.attr}")
    for name in imported_modules(tree(path)):
        if name.split(".")[0] in ("argparse", "floe_cli", "floe_web", "getopt"):
            problems.append(f"imports {name}")
    assert not problems


@pytest.mark.parametrize("path", UIS, ids=rel)
def test_uis_use_only_the_public_api(path):
    public = set(floe.__all__)
    problems = []
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.startswith("floe."):
                problems.append(f"line {node.lineno}: from {node.module} import ...")
            elif node.module == "floe":
                problems += [f"line {node.lineno}: floe.{a.name}" for a in node.names if a.name not in public]
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "floe" and node.attr not in public):
            problems.append(f"line {node.lineno}: floe.{node.attr}")
    assert not problems


def test_public_api_is_complete():
    for name in floe.__all__:
        assert hasattr(floe, name), name
