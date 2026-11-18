"""Read the small, explicitly allowlisted set of packaged UI assets."""

from functools import cache
from importlib.resources import files

ICON_NAMES = frozenset({"arrow-left", "check", "circle-dashed", "clock-3", "refresh-cw", "x"})


@cache
def icon_svg(name: str) -> str:
    """Read a trusted SVG once; never resolve a request path or arbitrary file."""
    if name not in ICON_NAMES:
        raise ValueError(f"unknown icon: {name!r}")
    return files("floe_web").joinpath("icons", f"{name}.svg").read_text(encoding="utf-8")


@cache
def logo_svg() -> str:
    """The original two-piece Floe mark, separate from the Lucide subset."""
    return files("floe_web").joinpath("logo.svg").read_text(encoding="utf-8").strip()
