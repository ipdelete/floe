import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--systemd-e2e", action="store_true", default=False,
        help="deploy generated floe-e2e-* workflows (requires the trusted live_e2e.py lease)",
    )


def pytest_configure(config):
    if config.getoption("--systemd-e2e"):
        from tools.agentic_dev_loop.live_e2e import Blocked, require_lease

        try:
            require_lease()
        except Blocked as exc:
            raise pytest.UsageError(str(exc)) from exc


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--systemd-e2e"):
        skip = pytest.mark.skip(reason="requires explicit --systemd-e2e opt-in")
        for item in items:
            if item.get_closest_marker("systemd_e2e"):
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _never_touch_real_unit_dir(tmp_path, monkeypatch, request):
    """Sandbox normal tests; only explicitly opted-in E2E uses real units."""
    if request.node.get_closest_marker("systemd_e2e") and request.config.getoption("--systemd-e2e"):
        return
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "test-config"))
