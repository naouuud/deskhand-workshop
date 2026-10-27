"""The Orders page's wiring in index.html, views.js and orders.js, read as
text. No browser and no server.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_logs_ui import _Page

STATIC = Path(__file__).resolve().parents[1] / "static"


@pytest.fixture(scope="module")
def page() -> _Page:
    parser = _Page()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return parser


def test_orders_is_the_tab_after_logs(page):
    assert page.tabs[:6] == ["run", "compare", "repeat", "incidents", "logs", "orders"]


def test_the_orders_view_exists_and_starts_hidden(page):
    view = page.views.get("view-orders")
    assert view is not None, "index.html has no #view-orders"
    assert "hidden" in view


def test_orders_js_loads_after_core_and_before_views(page):
    s = page.scripts
    assert s.index("core.js") < s.index("orders.js") < s.index("views.js")


def test_views_js_routes_orders():
    code = (STATIC / "views.js").read_text(encoding="utf-8")
    assert '"orders"' in re.search(r"var VIEWS = \[[^\]]*\]", code).group(0)
    assert "L.orders.show()" in code and "L.orders.hide()" in code


def test_orders_js_never_writes_markup():
    # O-70058's notes field is a prompt injection, and tickets are worse:
    # every value goes through L.el / textContent.
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", (STATIC / "orders.js").read_text(encoding="utf-8"), flags=re.S)
    assert "innerHTML" not in code
    assert "insertAdjacentHTML" not in code


def test_orders_js_is_served_no_cache():
    from fastapi.testclient import TestClient

    import server

    assert TestClient(server.app).get("/static/orders.js").headers.get("cache-control") == "no-cache"


def test_an_order_opens_in_a_dialog(page):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<dialog id="order-dialog"' in html
    for part in ("order-dialog-title", "order-dialog-body", "order-dialog-close"):
        assert f'id="{part}"' in html, part


def test_the_orders_view_keeps_the_ticket_picker():
    # The selected ticket's order is shown first, so the picker must stay.
    assert "body.view-orders .sidebar" not in (STATIC / "lab.css").read_text(encoding="utf-8")
    assert 'L.on("select"' in (STATIC / "orders.js").read_text(encoding="utf-8")


def test_the_ticket_strip_offers_the_order_on_every_view():
    # picker.js draws the strip that Run, Compare and Repeat share; its
    # View order button opens the Orders popup from there.
    picker = (STATIC / "picker.js").read_text(encoding="utf-8")
    assert '"View order "' in picker and "L.orders.view(id)" in picker
    assert "view: view" in (STATIC / "orders.js").read_text(encoding="utf-8")
