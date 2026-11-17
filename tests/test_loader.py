import sys
from types import ModuleType

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


@pytest.mark.parametrize("layout", ["module", "package", "namespace"])
@pytest.mark.parametrize("eager", [False, True])
def test_workflows_isolate_sibling_imports_at_load_and_task_time(tmp_path, monkeypatch, layout, eager):
    module_name = "floe_shared_helper"
    existing = ModuleType(module_name)
    existing.value = "outside"
    monkeypatch.setitem(sys.modules, module_name, existing)
    original_path = list(sys.path)
    workflows = []
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir()
        helper = directory / f"{module_name}.py"
        import_name = module_name
        if layout != "module":
            package = directory / module_name
            package.mkdir()
            if layout == "package":
                (package / "__init__.py").touch()
            helper = package / "steps.py"
            import_name += ".steps"
        helper.write_text(f'value = {name!r}\ncount = 0\n')
        path = directory / "flow.py"
        path.write_text(
            (f'from {import_name} import value\n' if eager else f'value = {name!r}\n')
            + f'WORKFLOW = {name!r}\n'
            'def step():\n'
            f'    import {import_name} as helper\n'
            '    helper.count += 1\n'
            '    return value, helper.value, helper.count\n'
            'tasks = {"s": step}\ndag = {"s": set()}\n'
        )
        workflows.append(load(path))
        assert sys.path == original_path
        assert sys.modules[module_name] is existing
        assert f"{module_name}.steps" not in sys.modules

    for count in (1, 2):
        for name, workflow in zip(("first", "second"), workflows):
            assert workflow.tasks["s"]() == (name, name, count)
            assert sys.path == original_path
            assert sys.modules[module_name] is existing
            assert f"{module_name}.steps" not in sys.modules


@pytest.mark.parametrize("fail_at", ["load", "task"])
def test_failed_workflow_restores_import_state(tmp_path, fail_at):
    module_name = "floe_failing_helper"
    (tmp_path / f"{module_name}.py").write_text('value = "local"\n')
    path = tmp_path / "flow.py"
    body = f'import {module_name}\nraise RuntimeError("boom")\n'
    if fail_at == "task":
        body = 'def step():\n' + ''.join(f'    {line}\n' for line in body.splitlines())
        body += 'WORKFLOW = "demo"\ntasks = {"s": step}\ndag = {"s": set()}\n'
    path.write_text(body)
    original_path = list(sys.path)
    with pytest.raises(LoadError if fail_at == "load" else RuntimeError, match="boom"):
        workflow = load(path)
        workflow.tasks["s"]()
    assert sys.path == original_path
    assert module_name not in sys.modules


def test_invalid_definition_raises_validation_error(tmp_path):
    path = tmp_path / "flow.py"
    path.write_text(VALID.replace('{"a": set()}', '{"a": {"b"}}'))
    with pytest.raises(ValidationError, match="unknown prerequisites"):
        load(path)
