"""The Saved state page's wiring in index.html, views.js, state.js and
lab.css, read as text. No browser and no server.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_logs_ui import _Page

STATIC = Path(__file__).resolve().parents[1] / "static"


def _code(name: str) -> str:
    """A static file with its comments removed."""
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", (STATIC / name).read_text(encoding="utf-8"), flags=re.S)


@pytest.fixture(scope="module")
def page() -> _Page:
    parser = _Page()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return parser


def test_saved_state_is_the_tab_after_orders(page):
    assert page.tabs[page.tabs.index("orders") + 1] == "state"


def test_the_state_view_exists_and_starts_hidden(page):
    view = page.views.get("view-state")
    assert view is not None, "index.html has no #view-state"
    assert "hidden" in view


def test_state_js_loads_after_core_and_before_views(page):
    s = page.scripts
    assert s.index("core.js") < s.index("state.js") < s.index("views.js")


def test_views_js_routes_state_and_hides_it():
    code = (STATIC / "views.js").read_text(encoding="utf-8")
    assert '"state"' in re.search(r"var VIEWS = \[[^\]]*\]", code).group(0)
    assert "L.state.show(h)" in code and "L.state.hide()" in code and "L.state.hash()" in code


def test_the_view_hides_the_ticket_picker():
    css = (STATIC / "lab.css").read_text(encoding="utf-8")
    assert "body.view-state .sidebar" in css
    assert 'classList.toggle("view-state"' in (STATIC / "views.js").read_text(encoding="utf-8")


def test_state_js_never_writes_markup():
    # The saved state holds ticket text (some tickets are prompt injections)
    # and model replies: every value goes through L.el / textContent.
    code = _code("state.js")
    assert "innerHTML" not in code
    assert "insertAdjacentHTML" not in code


def test_state_js_never_decides_what_a_restart_picks_up():
    # The answer is store.find_resumable()'s, through the server. A status
    # list in state.js would be a second copy of the rule.
    code = _code("state.js")
    assert "resumable" in code
    assert "TERMINAL" not in code


def test_state_css_uses_tokens_only():
    css = (STATIC / "lab.css").read_text(encoding="utf-8")
    block = css[css.index("/* ── Saved state view"):]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block)


def test_a_run_opens_in_a_dialog():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<dialog id="state-dialog"[^>]*>', html)
    assert m and 'aria-labelledby="state-dialog-title"' in m.group(0)
    assert 'id="state-dialog-close"' in html and 'aria-label="Close"' in html


def test_state_js_is_served_no_cache():
    from fastapi.testclient import TestClient

    import server

    assert TestClient(server.app).get("/static/state.js").headers.get("cache-control") == "no-cache"
