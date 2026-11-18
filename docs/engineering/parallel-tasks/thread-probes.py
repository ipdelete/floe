"""Reproducible, deliberately unsupported thread experiments; NOT a runner.

Run from the checkout with:
    uv run python docs/engineering/parallel-tasks/thread-probes.py

Each probe runs in a disposable interpreter with a deadline. No systemd calls.
Assertions describe expected observations, including failures of isolation.
"""

import contextlib
import io
import json
import os
import platform
import signal
import subprocess
import sys
import sysconfig
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import ModuleType

import floe


def wait(event):
    if not event.wait(3):
        raise TimeoutError("probe synchronization failed")


def imports_probe():
    """Two tasks of ONE loaded workflow, not competing workflow loads."""
    name = "thread_probe_helper"
    ambient = ModuleType(name)
    ambient.label, ambient.count = "ambient", -1
    sys.modules[name] = ambient
    original_path = list(sys.path)
    observations = []
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        (directory / f"{name}.py").write_text('label = "workflow"\ncount = 0\n')
        path = directory / "flow.py"
        path.write_text(
            f"import {name}\nWORKFLOW = 'probe'\n"
            "def step(action=lambda helper: (helper.label, helper.count)):\n"
            f"    import {name} as helper\n"
            "    helper.count += 1\n"
            "    return action(helper)\n"
            "tasks = {'a': step, 'b': step}\ndag = {'a': set(), 'b': set()}\n"
        )
        workflow = floe.load(path)
        assert workflow.tasks["a"]() == ("workflow", 1)
        assert workflow.tasks["b"]() == ("workflow", 2)
        for task in ("a", "b"):
            def fail(helper):
                raise RuntimeError("expected failure")
            try:
                workflow.tasks[task](fail)
            except RuntimeError as exc:
                assert str(exc) == "expected failure"
            else:
                raise AssertionError("missing control failure")
            assert sys.path == original_path and sys.modules[name] is ambient
        observations.append({"mode": "serial", "correct_helpers_and_restoration": True})

        for fails in (False, True):
            workflow = floe.load(path)
            a_entered, b_entered, a_exited = Event(), Event(), Event()
            seen = []

            def a(helper):
                assert helper.label == "workflow"
                a_entered.set()
                wait(b_entered)

            def b(helper):
                assert helper.label == "workflow"
                b_entered.set()
                wait(a_exited)
                imported = __import__(name)
                seen.append(imported.label)
                if fails:
                    raise RuntimeError("expected failure")
                return imported.label

            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(workflow.tasks["a"], a)
                wait(a_entered)
                second = pool.submit(workflow.tasks["b"], b)
                try:
                    first.result(timeout=4)
                finally:
                    a_exited.set()
                try:
                    second.result(timeout=4)
                except RuntimeError as exc:
                    assert fails and str(exc) == "expected failure"
                else:
                    assert not fails
            result = {
                "mode": "overlap", "second_task_raises": fails,
                "second_task_late_import": seen,
                "path_restored": sys.path == original_path,
                "ambient_module_restored": sys.modules.get(name) is ambient,
            }
            assert seen == ["ambient"]
            assert not result["path_restored"] and not result["ambient_module_restored"]
            observations.append(result)
            # Repair only this disposable interpreter's deliberately corrupted state.
            sys.path[:] = original_path
            sys.modules[name] = ambient
    return observations


def output_probe():
    """A thread-local Python sink does not localize descriptor 1 or children."""
    from threading import local

    routing = local()
    fallback = sys.stdout

    class RoutedStream:
        def write(self, text):
            return getattr(routing, "sink", fallback).write(text)

        def flush(self):
            return getattr(routing, "sink", fallback).flush()

    saved = os.dup(1)
    observations = []
    try:
        with tempfile.TemporaryFile(mode="w+b") as a_sink, tempfile.TemporaryFile(mode="w+b") as b_sink:
            def reset():
                for sink in (a_sink, b_sink):
                    sink.seek(0)
                    sink.truncate()

            def contents(sink):
                sink.seek(0)
                return sink.read().decode().splitlines()

            def writes():
                # Force three distinct sources to write to the inherited stdout.
                print("A-python", flush=True)
                os.write(1, b"A-raw\n")
                subprocess.run([sys.executable, "-c", "print('A-child', flush=True)"],
                               check=True, timeout=3)

            reset()
            python_a, python_b = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(RoutedStream()):
                routing.sink = python_a
                os.dup2(a_sink.fileno(), 1)
                writes()
                assert python_a.getvalue().splitlines() == ["A-python"]
                assert contents(a_sink) == ["A-raw", "A-child"]
                observations.append({"mode": "serial", "python": ["A-python"],
                                     "descriptor_A": contents(a_sink)})

                reset()
                a_entered, b_redirected, a_written = Event(), Event(), Event()

                def a():
                    routing.sink = python_a
                    os.dup2(a_sink.fileno(), 1)
                    a_entered.set()
                    wait(b_redirected)
                    try:
                        writes()
                    finally:
                        a_written.set()

                def b():
                    routing.sink = python_b
                    wait(a_entered)
                    os.dup2(b_sink.fileno(), 1)
                    b_redirected.set()
                    wait(a_written)

                python_a.seek(0)
                python_a.truncate()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first, second = pool.submit(a), pool.submit(b)
                    first.result(timeout=4)
                    second.result(timeout=4)
                assert python_a.getvalue().splitlines() == ["A-python"]
                assert python_b.getvalue() == ""
                assert contents(a_sink) == []
                assert contents(b_sink) == ["A-raw", "A-child"]
                observations.append({"mode": "overlap", "python_A": ["A-python"],
                                     "descriptor_A": contents(a_sink),
                                     "descriptor_B": contents(b_sink)})
    finally:
        os.dup2(saved, 1)
        os.close(saved)
    return observations


def cancellation_probe():
    """Cancellation is NOT termination; explicitly release/reap our child."""
    started, release = Event(), Event()
    children = []

    def task():
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        children.append(child)
        try:
            started.set()
            wait(release)
        finally:
            child.terminate()
            child.wait(timeout=3)

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(task)
    try:
        wait(started)
        cancelled = future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        result = {"cancel_returned": cancelled, "task_still_running": future.running(),
                  "child_still_running": children[0].poll() is None}
        assert result == {"cancel_returned": False, "task_still_running": True,
                          "child_still_running": True}
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)
        # Also clean up if the worker raised while trying to reap its own child.
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
    future.result(timeout=1)
    result["cooperative_release_and_child_reap"] = children[0].returncode is not None
    assert result["cooperative_release_and_child_reap"]
    return result


PROBES = {"imports": imports_probe, "output": output_probe, "cancellation": cancellation_probe}


def main():
    if len(sys.argv) == 2:
        print(json.dumps(PROBES[sys.argv[1]]()))
        return
    result = {"environment": {"python": sys.version, "platform": platform.platform(),
                              "gil_disabled_build": sysconfig.get_config_var("Py_GIL_DISABLED"),
                              "gil_enabled": sys._is_gil_enabled()}}
    for name in PROBES:
        # Own process group, so even a failed probe's child cannot outlive a deadline.
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), name],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=20)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
            raise
        if process.returncode:
            raise RuntimeError(f"{name}: {process.returncode}\n{stdout}\n{stderr}")
        result[name] = json.loads(stdout)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
