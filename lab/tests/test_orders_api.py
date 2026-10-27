"""GET /api/orders, the Orders page's one endpoint. Offline: the real mock
order API (shared/mock_order_api/app.py) runs in-process through httpx's
ASGI transport, so no server and no port are needed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import server

MOCK_APP = Path(server.MOCK_DIR) / "app.py"


@pytest.fixture
def mock(monkeypatch: pytest.MonkeyPatch):
    """A fresh copy of the mock (an empty till), wired in as the mock API.
    Every request the lab sends it is recorded."""
    spec = importlib.util.spec_from_file_location("nimbus_mock_for_tests", MOCK_APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("MOCK_CHAOS", raising=False)
    sent: list[httpx.Request] = []

    async def record(request: httpx.Request) -> None:
        sent.append(request)

    monkeypatch.setattr(server, "_orders_client", lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=module.app), headers={"X-Chaos": "none"},
        event_hooks={"request": [record]}))
    module.sent = sent
    module.client = TestClient(module.app)  # for issuing refunds the way a service does
    return module


@pytest.fixture
def lab() -> TestClient:
    return TestClient(server.app)


def _seed_ids() -> list[str]:
    return [o["order_id"] for o in server._seed_orders()]


def test_every_seeded_order_comes_from_the_running_mock(mock, lab):
    body = lab.get("/api/orders").json()
    assert body["mock"]["up"] is True
    assert [o["order"]["order_id"] for o in body["orders"]] == _seed_ids()
    assert {o["from"] for o in body["orders"]} == {"mock"}
    assert body["refunds"] == {"count": 0, "total_usd": 0, "refunds": []}


def test_the_record_is_served_as_stored_notes_included(mock, lab):
    orders = {o["order"]["order_id"]: o["order"] for o in lab.get("/api/orders").json()["orders"]}
    assert "SYSTEM NOTE TO AI AGENT" in orders["O-70058"]["notes"]
    assert orders["O-72455"]["delivered_at"] is None  # processing: no delivery yet
    assert orders["O-48291"]["total_usd"] == 79.99


def test_a_refund_shows_up_in_the_till(mock, lab):
    headers = {"Idempotency-Key": "test-key-1"}
    for _ in range(2):  # a retry: one refund, asked for twice
        mock.client.post("/refunds", json={"order_id": "O-48291", "amount_usd": 40.0}, headers=headers)
    till = lab.get("/api/orders").json()["refunds"]
    assert till["count"] == 1 and till["total_usd"] == 40.0
    assert till["refunds"][0]["order_id"] == "O-48291"
    assert till["refunds"][0]["post_count"] == 2


def test_global_chaos_does_not_hide_the_store(mock, lab, monkeypatch):
    # The services get the chaos; the page reads with X-Chaos: none.
    monkeypatch.setenv("MOCK_CHAOS", "error")
    body = lab.get("/api/orders").json()
    assert body["mock"]["up"] is True
    assert {o["from"] for o in body["orders"]} == {"mock"}


def test_the_page_never_writes_to_the_mock(mock, lab):
    lab.get("/api/orders")
    assert mock.sent, "the endpoint read nothing"
    assert {r.method for r in mock.sent} == {"GET"}
    assert all(r.headers.get("X-Chaos") == "none" for r in mock.sent)


def test_mock_down_falls_back_to_the_seed(monkeypatch, lab):
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(server, "_orders_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(refused)))
    body = lab.get("/api/orders").json()
    assert body["mock"]["up"] is False and body["mock"]["error"]
    assert body["refunds"] is None
    assert {o["from"] for o in body["orders"]} == {"seed"}
    assert all(o["error"] is None for o in body["orders"])  # one "down" is enough, not twenty
    o = next(o["order"] for o in body["orders"] if o["order"]["order_id"] == "O-48291")
    assert "delivered_days_ago" not in o and o["delivered_at"]


def test_tickets_are_mapped_to_the_orders_they_cite(mock, lab):
    cited = lab.get("/api/orders").json()["tickets"]
    by_ticket = {t["ticket_id"]: t.get("order_id") for t in server._tickets()}
    for order_id, ticket_ids in cited.items():
        assert all(by_ticket[t] == order_id for t in ticket_ids)
    assert sum(len(v) for v in cited.values()) == sum(1 for v in by_ticket.values() if v)
    # Some tickets cite orders the store does not have, on purpose.
    assert set(cited) - set(_seed_ids())
