"""Mock Nimbus Electronics order API.

Stands in for the real order system in every checkpoint. In-memory only —
restart it to reset refunds. Chaos modes simulate the real API misbehaving.
GET /refunds is the till: every refund issued since the process started.
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

SEED_PATH = Path(__file__).parent / "orders_seed.json"
# Failure injection: each mode is one way a real dependency lets you down.
# The workshop code that answers it lives in nodes.py from 02-service on
# (_order_api_call: 5s timeout, 2 retries with 0.5s/1.0s backoff on timeouts,
# connection errors and 5xx) and in rules.py. v1 never calls this API.
#   timeout       the API hangs. Retried, then TOOL_ORDER_API_TIMEOUT.
#   error         the API is down. Retried, then TOOL_ORDER_API_ERROR.
#   flaky         half the calls fail. Retries usually recover: all three
#                 attempts fail only 1 time in 8.
#   inconsistent  the API answers 200 with an incomplete record (no total_usd).
#                 No retry helps; rule 1 in rules.py refuses a refund it cannot
#                 check against the order total.
CHAOS_MODES = {"none", "timeout", "error", "flaky", "inconsistent"}


def _load_orders() -> dict[str, dict]:
    """Load the seed, turning relative `delivered_days_ago` into absolute ISO
    timestamps at startup — so "delivered 12 days ago" stays true whenever the
    class runs, not just the day the seed was written."""
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    orders: dict[str, dict] = {}
    for order in seed["orders"]:
        days_ago = order.pop("delivered_days_ago")
        order["delivered_at"] = (
            (now - timedelta(days=days_ago)).isoformat()
            if days_ago is not None
            else None
        )
        orders[order["order_id"]] = order
    return orders


app = FastAPI(title="Nimbus Mock Order API")
ORDERS = _load_orders()
# Module globals are the whole database: one uvicorn process, one till. A
# restart forgets every refund, and a second worker process would keep its own.
REFUNDS: dict[str, dict] = {}  # Idempotency-Key -> refund record
# Idempotency-Key -> when the refund was issued and how many POSTs asked for it
# (2+ means a retry got the original back). Kept apart so POST's reply is unchanged.
REFUND_LEDGER: dict[str, dict] = {}


def _chaos_mode(request: Request) -> str:
    """Per-request X-Chaos header wins over the MOCK_CHAOS env var."""
    mode = request.headers.get("X-Chaos") or os.getenv("MOCK_CHAOS", "none")
    # A typo is a loud 400, not a silent "none". A 4xx is an answer to
    # _order_api_call, so the services do not retry it.
    if mode not in CHAOS_MODES:
        raise HTTPException(
            400, f"unknown chaos mode {mode!r}, expected one of {sorted(CHAOS_MODES)}"
        )
    return mode


def _apply_chaos(mode: str) -> None:
    """Misbehave before the endpoint does its work. "inconsistent" is not
    handled here: it corrupts the response, so get_order applies it."""
    if mode == "timeout":
        # 30s is far past the services' 5s client timeout. The endpoints are
        # plain `def`, so FastAPI runs them in a thread pool and one hanging
        # request does not block the others. After the sleep the request
        # carries on as normal, which matters for POST /refunds below.
        time.sleep(30)
    elif mode == "error":
        raise HTTPException(500, "chaos: simulated internal error")
    elif (
        mode == "flaky" and random.random() < 0.5
    ):  # unseeded: a different pattern every run
        raise HTTPException(500, "chaos: simulated flaky failure")


@app.get("/health")
def check_health() -> str:
    return "All good."


@app.get("/orders/{order_id}")
def get_order(order_id: str, request: Request) -> dict:
    mode = _chaos_mode(request)
    _apply_chaos(mode)
    order = ORDERS.get(order_id)
    if order is None:
        # Some tickets cite orders that do not exist on purpose (typos, ancient
        # orders). lookup_order turns this 404 into None, an answer and not a
        # failure, and the plan must then ask for details instead of refunding.
        raise HTTPException(404, "order not found")
    # The record goes out as stored, free-text `notes` included. O-70058's note
    # is an indirect prompt injection: an instruction aimed at the model, in
    # data the services fetch themselves. 04-production screens the record in
    # fetch_order_data; 02-service and 03-rag hand it to the planner unscreened.
    payload = dict(order)  # a copy, so the chaos below never edits ORDERS
    if mode == "inconsistent":
        payload.pop("total_usd")  # the "real API returns garbage sometimes" scenario
    return payload


class RefundRequest(BaseModel):
    order_id: str
    amount_usd: float


@app.post("/refunds")
def issue_refund(
    body: RefundRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Issue a refund, at most once per Idempotency-Key.

    The idempotency key is the client's name for "this one refund". The
    services derive it as request_id:action_index, so a retry or a re-run after
    a crash sends the same key and gets the original refund back (incident #9).

    This endpoint protects against paying twice and nothing else. It does not
    check that the order exists, that the amount fits the order total, or that
    a repeated key carries the same body. Those checks are Deskhand's job
    (rules.py, the approval gate), which is the point: the money is only as
    safe as the code in front of this call.
    """
    # Chaos runs first. "error" and "flaky" fail before anything is recorded,
    # so a failed POST issued nothing. "timeout" is the nasty one: the client
    # gives up after 5s, but this request wakes at 30s and issues the refund
    # anyway. A timeout is not a "no". Every retry carries the same key, so
    # however many arrive, the till shows one refund.
    _apply_chaos(_chaos_mode(request))
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key header is required")
    # A plain check-then-insert. Two same-key requests racing in the thread
    # pool could both miss; a real payment API locks on the key. Fine for class.
    if idempotency_key in REFUNDS:
        REFUND_LEDGER[idempotency_key]["post_count"] += 1
        return REFUNDS[
            idempotency_key
        ]  # same key => same refund, never a double refund
    refund = {
        "refund_id": f"R-{uuid.uuid4().hex[:8]}",
        "order_id": body.order_id,
        "amount_usd": body.amount_usd,
        "status": "issued",
    }
    REFUNDS[idempotency_key] = refund
    REFUND_LEDGER[idempotency_key] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "post_count": 1,
    }
    return refund


@app.get("/refunds")
def list_refunds() -> dict:
    """Read-only: every refund issued since this process started, oldest first,
    so a class can count the money directly instead of searching logs.

    Chaos modes never apply here (no X-Chaos, no MOCK_CHAOS): this is the
    instructor's view of the till, not part of the API the services call, and
    a chaos error or a 30s hang must never hide money that moved. A replayed
    Idempotency-Key is still one refund (post_count says how often it was asked)."""
    refunds = [
        {**refund, "idempotency_key": key, **REFUND_LEDGER[key]}
        for key, refund in REFUNDS.items()
    ]
    return {
        "count": len(refunds),
        "total_usd": round(sum(r["amount_usd"] for r in refunds), 2),
        "refunds": refunds,
    }
