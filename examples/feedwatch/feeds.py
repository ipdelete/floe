"""Fetch and parse Atom and RSS feeds with the standard library only."""

import email.utils
import hashlib
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

ATOM = "{http://www.w3.org/2005/Atom}"
USER_AGENT = "floe-feedwatch/0.1"
MAX_BYTES = 5_000_000
SUMMARY_CHARS = 400


@dataclass(frozen=True)
class Item:
    key: str            # stable identity across runs: "<feed name>|<entry id>"
    feed: str
    title: str
    link: str
    published: str | None  # ISO 8601 UTC, when the feed provides a date
    summary: str

    def to_dict(self) -> dict:
        return asdict(self)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(markup: str | None, limit: int = SUMMARY_CHARS) -> str:
    if not markup:
        return ""
    parser = _Text()
    parser.feed(markup)
    text = " ".join(" ".join(parser.parts).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _atom_date(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return _iso(datetime.fromisoformat(text.strip()))
    except ValueError:
        return None


def _rss_date(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return _iso(email.utils.parsedate_to_datetime(text.strip()))
    except (TypeError, ValueError):
        return None


def _text(element, path: str) -> str:
    found = element.find(path)
    return (found.text or "").strip() if found is not None else ""


def _key(feed: str, entry_id: str, title: str) -> str:
    if not entry_id:
        entry_id = "sha1:" + hashlib.sha1(title.encode()).hexdigest()
    return f"{feed}|{entry_id}"


def parse(feed: str, data: bytes) -> list[Item]:
    root = ET.fromstring(data)
    items = []
    if root.tag == f"{ATOM}feed":
        for entry in root.findall(f"{ATOM}entry"):
            title = _text(entry, f"{ATOM}title")
            link = ""
            for candidate in entry.findall(f"{ATOM}link"):
                if candidate.get("rel", "alternate") == "alternate":
                    link = candidate.get("href", "")
                    break
            summary = entry.find(f"{ATOM}summary")
            if summary is None:
                summary = entry.find(f"{ATOM}content")
            items.append(Item(
                key=_key(feed, _text(entry, f"{ATOM}id") or link, title),
                feed=feed,
                title=title,
                link=link,
                published=_atom_date(_text(entry, f"{ATOM}published") or _text(entry, f"{ATOM}updated")),
                summary=plain_text(summary.text if summary is not None else ""),
            ))
    elif root.tag == "rss":
        for entry in root.findall("channel/item"):
            title = _text(entry, "title")
            link = _text(entry, "link")
            items.append(Item(
                key=_key(feed, _text(entry, "guid") or link, title),
                feed=feed,
                title=title,
                link=link,
                published=_rss_date(_text(entry, "pubDate")),
                summary=plain_text(_text(entry, "description")),
            ))
    else:
        raise ValueError(f"not an Atom or RSS document (root element {root.tag!r})")
    return items


def fetch(feed: str, url: str, timeout: float = 20) -> list[Item]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"feed larger than {MAX_BYTES} bytes")
    return parse(feed, data)
