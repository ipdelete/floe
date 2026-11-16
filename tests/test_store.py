from pathlib import Path

import pytest

from floe.store import NotOwnedError, UnitStore

OWNED = "# Managed by floe\n[Service]\nExecStart=/bin/true\n\n[X-Floe]\nOwner=floe\n"


def test_write_is_atomic_idempotent_and_removable(tmp_path):
    store = UnitStore(tmp_path / "units")
    assert store.write("demo.service", OWNED)
    file = store.path / "demo.service"
    assert file.read_text() == OWNED
    assert file.stat().st_mode & 0o777 == 0o644
    assert store.is_owned("demo.service")
    mtime = file.stat().st_mtime_ns
    assert not store.write("demo.service", OWNED)
    assert file.stat().st_mtime_ns == mtime
    assert store.remove("demo.service")
    assert not store.remove("demo.service")
    assert list(store.path.iterdir()) == []


@pytest.mark.parametrize("foreign", [
    "not even valid unit syntax",
    "# Owner=floe\n[Service]\nExecStart=/bin/true\n",
    "[Other]\nOwner=floe\n",
    "[X-Floe]\nOwner=floe\nOwner=other\n",
])
def test_foreign_files_need_explicit_adoption(tmp_path, foreign):
    file = tmp_path / "demo.service"
    file.write_text(foreign)
    store = UnitStore(tmp_path)
    assert not store.is_owned(file.name)
    with pytest.raises(NotOwnedError):
        store.write(file.name, OWNED)
    with pytest.raises(NotOwnedError):
        store.remove(file.name)
    assert file.read_text() == foreign
    assert store.write(file.name, OWNED, adopt=True)
    assert store.is_owned(file.name)


@pytest.mark.parametrize("destination", ["mask", "dangling", "directory", "owned-link"])
def test_non_regular_destinations_refused_even_with_adopt(tmp_path, destination):
    file = tmp_path / "demo.service"
    if destination == "directory":
        file.mkdir()
    else:
        target = {"mask": Path("/dev/null"), "dangling": tmp_path / "absent",
                  "owned-link": tmp_path / "owned"}[destination]
        if destination == "owned-link":
            target.write_text(OWNED)
        file.symlink_to(target)
    store = UnitStore(tmp_path)
    assert not store.is_owned(file.name)
    with pytest.raises(NotOwnedError):
        store.preflight(file.name, adopt=True)
    with pytest.raises(NotOwnedError):
        store.write(file.name, OWNED, adopt=True)
    with pytest.raises(NotOwnedError):
        store.remove(file.name)


@pytest.mark.parametrize("name", ["../escape.service", "x.conf", "-x.service", "x@.service", ".x.service"])
def test_store_rejects_unsafe_names(tmp_path, name):
    store = UnitStore(tmp_path)
    with pytest.raises(ValueError):
        store.preflight(name)


def test_delete_removes_only_links_targeting_deleted_unit(tmp_path):
    store = UnitStore(tmp_path)
    store.write("demo.timer", OWNED)
    for directory, target in [("timers.target.wants", "../demo.timer"),
                              ("other.target.requires", "/elsewhere/demo.timer")]:
        parent = tmp_path / directory
        parent.mkdir()
        (parent / "demo.timer").symlink_to(target)
    assert store.remove("demo.timer")
    assert not (tmp_path / "timers.target.wants" / "demo.timer").is_symlink()
    assert (tmp_path / "other.target.requires" / "demo.timer").is_symlink()


def test_failed_atomic_replace_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    store = UnitStore(tmp_path)
    store.write("demo.service", OWNED)

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr("floe.store.os.replace", fail)
    with pytest.raises(OSError, match="disk error"):
        store.write("demo.service", OWNED + "# changed\n")
    assert (tmp_path / "demo.service").read_text() == OWNED
    assert [p.name for p in tmp_path.iterdir()] == ["demo.service"]
