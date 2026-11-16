import pytest


@pytest.fixture(autouse=True)
def _never_touch_real_unit_dir(tmp_path, monkeypatch):
    """Point the default unit directory somewhere harmless for every test."""
    import floe.deploy

    monkeypatch.setattr(floe.deploy, "USER_UNIT_DIR", tmp_path / "default-unit-dir-must-not-be-used")
