import sys

import pytest

from floe.loader import load
from floe.model import LoadError, ValidationError

VALID = 'WORKFLOW = "demo"\nSCHEDULE = ["daily"]\ntasks = {"a": print}\ndag = {"a": set()}\n'


def test_load_definition(tmp_path):
    path = tmp_path / "flow.py"
    path.write_text(VALID)
    workflow = load(path)
    assert workflow.name == "demo"
    assert workflow.schedule == ("daily",)
    assert workflow.source == path.resolve()
    assert workflow.order == ("a",)


def test_schedule_optional_and_main_block_not_run(tmp_path):
    path = tmp_path / "flow.py"
    path.write_text('WORKFLOW = "demo"\ntasks = {}\ndag = {}\n'
                    'if __name__ == "__main__":\n    raise SystemExit("ran main")\n')
    assert load(path).schedule == ()


def test_sibling_modules_importable(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "helpers_for_floe_test.py").write_text("def step():\n    pass\n")
    path = tmp_path / "flow.py"
    path.write_text('from helpers_for_floe_test import step\nWORKFLOW = "demo"\ntasks = {"s": step}\ndag = {"s": set()}\n')
    assert load(path).order == ("s",)


@pytest.mark.parametrize("source,match", [
    ("raise RuntimeError('boom')", "RuntimeError: boom"),
    ("def broken(:", "SyntaxError"),
    ('WORKFLOW = "demo"\n', "missing tasks, dag"),
])
def test_load_errors(tmp_path, source, match):
    path = tmp_path / "flow.py"
    path.write_text(source)
    with pytest.raises(LoadError, match=match):
        load(path)


def test_missing_file(tmp_path):
    with pytest.raises(LoadError, match="not found"):
        load(tmp_path / "absent.py")


def test_invalid_definition_raises_validation_error(tmp_path):
    path = tmp_path / "flow.py"
    path.write_text(VALID.replace('{"a": set()}', '{"a": {"b"}}'))
    with pytest.raises(ValidationError, match="unknown prerequisites"):
        load(path)
