"""Refund execution is idempotent: re-running execute_actions after a crash
must not pay twice, because the Idempotency-Key is derived from request_id +
action index and the order API returns the original refund for a known key."""
from __future__ import annotations

import nodes
from models import Action, ResolutionPlan, TicketResolutionState


def _no_emit(event: str, data: dict) -> int:
    return 0


def make_state() -> TicketResolutionState:
    plan = ResolutionPlan(
        plan_version="v1",
        ticket_id="T-1001",
        category="refund_request",
        actions=[Action(type="issue_refund", params={"amount_usd": 30.0})],
        refund_amount_usd=30.0,
        requires_approval=False,
        confidence=0.9,
        citations=[],
        rationale="refund within policy",
    )
    return TicketResolutionState(
        request_id="req-abc123",
        ticket_id="T-1001",
        ticket_text="refund please, order O-48291",
        order_data={"order_id": "O-48291", "total_usd": 79.99},
        resolution_plan=plan,
        status="running",
    )


# The key is derived from the run (request_id:action_index), not generated per
# call and not written by the model, so a retry of the same action is recognised.
def test_refund_carries_derived_idempotency_key(order_api) -> None:
    state = make_state()
    next_node = nodes.execute_actions(state, _no_emit)

    assert next_node == "draft_customer_reply"
    assert list(order_api.refunds_by_key) == ["req-abc123:0"]
    assert order_api.refunds_by_key["req-abc123:0"]["amount_usd"] == 30.0
    assert "refund R-0001 issued" in (state.internal_note or "")


def test_rerunning_execute_actions_does_not_double_refund(
    order_api,
) -> None:
    first = make_state()
    nodes.execute_actions(first, _no_emit)

    # Simulate a crash-and-retry: the SAME run executes the node again.
    retried = make_state()
    nodes.execute_actions(retried, _no_emit)

    assert order_api.refund_post_count == 2  # the call happened twice...
    assert len(order_api.refunds_by_key) == 1  # ...but only one refund exists
    refund_ids = {r["refund_id"] for r in order_api.refunds_by_key.values()}
    assert refund_ids == {"R-0001"}


def test_distinct_runs_get_distinct_refunds(order_api) -> None:
    # The other half of the contract: the key must not be too stable either.
    # A different run is a different refund, not a replay of the first.
    first = make_state()
    nodes.execute_actions(first, _no_emit)

    other = make_state()
    other.request_id = "req-other999"
    nodes.execute_actions(other, _no_emit)

    assert len(order_api.refunds_by_key) == 2
