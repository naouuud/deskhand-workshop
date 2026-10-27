"""The Logs page's wiring in index.html and logs.js, read as text (spec R1 and
Limits). No browser and no server.

What the page does in a browser (live tail, both themes, focus rings) is the
manual list in the spec's "Done when".
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "static"


class _Page(HTMLParser):
    """The bits of index.html these tests need: tabs, views, script order."""

    def __init__(self) -> None:
        super().__init__()
        self.tabs: list[str] = []
        self.views: dict[str, dict] = {}
        self.scripts: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "button" and "tab" in (a.get("class") or "").split():
            self.tabs.append(a.get("data-view"))
        if (a.get("id") or "").startswith("view-"):
            self.views[a["id"]] = a
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"].rsplit("/", 1)[-1])


@pytest.fixture(scope="module")
def page() -> _Page:
    parser = _Page()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return parser


def test_logs_is_the_fifth_tab_after_incidents(page):
    assert page.tabs[:5] == ["run", "compare", "repeat", "incidents", "logs"]


def test_the_logs_view_exists_and_starts_hidden(page):
    view = page.views.get("view-logs")
    assert view is not None, "index.html has no #view-logs"
    assert "hidden" in view


def test_logs_js_loads_after_core_and_before_views(page):
    s = page.scripts
    assert "logs.js" in s
    assert s.index("core.js") < s.index("logs.js") < s.index("views.js")


def test_the_page_and_its_scripts_are_revalidated_on_every_load():
    # A cached views.js from before the Logs tab existed sends a click on
    # Logs to the Run view. no-cache makes the browser check every load.
    from fastapi.testclient import TestClient

    import server

    client = TestClient(server.app)
    for path in ("/", "/static/index.html", "/static/views.js", "/static/logs.js"):
        assert client.get(path).headers.get("cache-control") == "no-cache", path


def test_logs_js_never_writes_markup():
    # Replies, rationales and guard reasons carry model text and injection
    # payloads: text goes through L.el / textContent only.
    path = STATIC / "logs.js"
    assert path.exists(), "lab/static/logs.js does not exist yet"
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text(encoding="utf-8"), flags=re.S)
    assert "innerHTML" not in code
    assert "insertAdjacentHTML" not in code
