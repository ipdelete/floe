"""The vendored icon subset is inert, packaged, and always paired with text."""

from html.parser import HTMLParser
from importlib.resources import files
from xml.etree import ElementTree

import pytest

from floe_web import render
from floe_web.assets import ICON_NAMES, icon_svg


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.words = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        if data.strip():
            self.words.append(data.strip())


def test_exact_svg_subset_and_license_are_distributed_as_package_resources():
    expected = {"arrow-left", "check", "circle-dashed", "clock-3", "refresh-cw", "x"}
    directory = files("floe_web").joinpath("icons")
    assert ICON_NAMES == expected
    assert {file.name for file in directory.iterdir() if file.name.endswith(".svg")} == {
        f"{name}.svg" for name in expected}
    license = directory.joinpath("LICENSE").read_text()
    assert "ISC License" in license and "The MIT License (MIT)" in license
    assert "Lucide Icons and Contributors" in license and "Cole Bemis" in license
    assert "lucide-static@1.74.0" in directory.joinpath("README.md").read_text()


@pytest.mark.parametrize("name", sorted(ICON_NAMES))
def test_svg_geometry_is_unchanged_and_decorative(name):
    original = ElementTree.fromstring(icon_svg(name))
    rendered = ElementTree.fromstring(render.icon(name))
    assert rendered.get("aria-hidden") == "true"
    assert rendered.get("focusable") == "false"
    assert rendered.get("viewBox") == "0 0 24 24"
    assert rendered.get("stroke") == "currentColor" and rendered.get("stroke-width") == "2"
    assert [(node.tag, node.attrib) for node in original] == [(node.tag, node.attrib) for node in rendered]
    for node in rendered.iter():
        assert node.tag.rsplit("}", 1)[-1] in ("svg", "path", "circle")
        assert not any(key.startswith("on") or "href" in key for key in node.attrib)


@pytest.mark.parametrize("name", ["../LICENSE", "/etc/passwd", "check.svg", "logo", ""])
def test_only_allowlisted_assets_can_be_read(name):
    with pytest.raises(ValueError, match="unknown icon"):
        icon_svg(name)


@pytest.mark.parametrize("state,name,label", [
    ("succeeded", "check", "Succeeded"),
    ("failed", "x", "Failed"),
    ("running", "clock-3", "Running"),
    ("incomplete", "circle-dashed", "Incomplete"),
])
def test_status_icons_have_visible_labels_and_inherit_the_status_class(state, name, label):
    markup = Markup(render.status(state))
    assert markup.words == [label]
    assert markup.elements[0] == ("span", {"class": state})
    svgs = [attrs for tag, attrs in markup.elements if tag == "svg"]
    assert len(svgs) == 1 and f"lucide-{name}" in svgs[0]["class"]
    assert svgs[0]["aria-hidden"] == "true" and svgs[0]["focusable"] == "false"
    heading = Markup(render.status(state, label="Run " + state))
    assert heading.words == ["Run " + state]


def test_navigation_icons_do_not_replace_labels_or_add_controls():
    back = Markup(render.back("/", "Workflows"))
    assert back.words == ["Workflows"]
    assert [(tag, attrs) for tag, attrs in back.elements if tag == "a"] == [("a", {"href": "/"})]
    assert any("lucide-arrow-left" in attrs.get("class", "") for tag, attrs in back.elements if tag == "svg")
    page = Markup(render.document("Workflows", "<h1>Workflows</h1>", "/").decode())
    assert "Refresh" in page.words and "Floe" in page.words
    assert any("lucide-refresh-cw" in attrs.get("class", "") for tag, attrs in page.elements if tag == "svg")
    assert not any(tag in ("script", "img", "button") for tag, _ in page.elements)


def test_unknown_status_stays_text_only_and_escaped():
    markup = Markup(render.status("unknown <script>"))
    assert markup.words == ["Unknown <script>"]
    assert not any(tag in ("svg", "script") for tag, _ in markup.elements)
