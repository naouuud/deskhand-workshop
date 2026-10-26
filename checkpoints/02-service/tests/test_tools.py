"""The two tools as LangChain tools: a schema plus a promise.

`lookup_order` and `issue_refund` are StructuredTools. Their argument schema
is validated BEFORE any HTTP happens, their retry policy lives inside them,
and their failures are taxonomy-coded. The order API is an httpx
MockTransport (see conftest.py), so no server is needed.
"""
from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

import errors
import nodes


def test_tools_expose_schemas_a_model_could_read() -> None:
    # What a model would see if the tools were handed to it with bind_tools.
    # The "Write" description marks the one that moves money.
    assert [t.name for t in nodes.TOOLS] == ["lookup_order", "issue_refund"]
    lookup = nodes.lookup_order.args_schema.model_json_schema()
    refund = nodes.issue_refund.args_schema.model_json_schema()
    assert lookup["required"] == ["order_id"]
    assert set(refund["required"]) == {"order_id", "amount_usd", "idempotency_key"}
    assert nodes.lookup_order.description.startswith("Read-only")
    assert nodes.issue_refund.description.startswith("Write")


def test_lookup_order_returns_record_or_none(order_api) -> None:
    assert nodes.lookup_order.invoke({"order_id": "O-48291"})["total_usd"] == 79.99
    assert nodes.lookup_order.invoke({"order_id": "O-00000"}) is None  # 404 is an answer


def test_issue_refund_sends_the_idempotency_key(order_api) -> None:
    refund = nodes.issue_refund.invoke(
        {"order_id": "O-48291", "amount_usd": 30.0, "idempotency_key": "req-1:0"}
    )
    assert refund["refund_id"] == "R-0001"
    assert list(order_api.refunds_by_key) == ["req-1:0"]


@pytest.mark.parametrize(
    "args",
    [
        {"order_id": "O-48291", "amount_usd": 0, "idempotency_key": "k"},
        {"order_id": "O-48291", "amount_usd": None, "idempotency_key": "k"},
        {"order_id": "48291", "amount_usd": 10.0, "idempotency_key": "k"},
        {"order_id": "O-48291", "amount_usd": 10.0, "idempotency_key": ""},
    ],
    ids=["zero-amount", "missing-amount", "bad-order-id", "empty-key"],
)
def test_bad_arguments_are_rejected_before_any_http(order_api, args) -> None:
    # The args_schema is the tool's own guard, whoever calls it. An empty key
    # would make a retry indistinguishable from a second refund.
    with pytest.raises(ValidationError):
        nodes.issue_refund.invoke(args)
    assert order_api.refund_post_count == 0


def test_retry_policy_lives_inside_the_tool(monkeypatch) -> None:
    # Bounded retry with backoff: two 503s, then success. The backoff is set
    # to zero so the test does not sleep.
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="try later")
        return httpx.Response(200, json={"order_id": "O-123", "total_usd": 10.0})

    monkeypatch.setattr(nodes, "transport", httpx.MockTransport(flaky))
    monkeypatch.setattr(nodes, "ORDER_API_BACKOFF_S", (0, 0))
    assert nodes.lookup_order.invoke({"order_id": "O-123"})["total_usd"] == 10.0
    assert calls["n"] == 3  # first attempt + 2 retries


def test_exhausted_timeouts_become_a_taxonomy_code(monkeypatch) -> None:
    # Once the retries are spent, what leaves the tool is a DeskhandError with
    # a code, never a raw httpx exception.
    def always_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("order API too slow", request=request)

    monkeypatch.setattr(nodes, "transport", httpx.MockTransport(always_timeout))
    monkeypatch.setattr(nodes, "ORDER_API_BACKOFF_S", (0, 0))
    with pytest.raises(errors.DeskhandError) as info:
        nodes.lookup_order.invoke({"order_id": "O-123"})
    assert info.value.internal_error_code == errors.TOOL_ORDER_API_TIMEOUT


def test_rejected_refund_becomes_a_taxonomy_code(monkeypatch) -> None:
    # A 4xx is an answer, so it is not retried; a refused write still fails
    # the run with a code instead of being treated as paid.
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "amount exceeds order total"})

    monkeypatch.setattr(nodes, "transport", httpx.MockTransport(reject))
    with pytest.raises(errors.DeskhandError) as info:
        nodes.issue_refund.invoke(
            {"order_id": "O-123", "amount_usd": 999.0, "idempotency_key": "k"}
        )
    assert info.value.internal_error_code == errors.TOOL_ORDER_API_ERROR
