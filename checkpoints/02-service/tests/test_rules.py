"""Unit tests for the business-rule validator (rules.py)."""
from __future__ import annotations

import rules
from models import Action, ResolutionPlan

ORDER = {"order_id": "O-48291", "customer_name": "Dana Whitfield", "total_usd": 79.99}
MISMATCHED_ORDER = {**ORDER, "customer_name": "Someone Else"}


# A valid $30 refund against ORDER (Dana's $79.99) by default. Each test
# overrides only the field it is about, so a failure points at one rule.
def make_plan(**overrides) -> ResolutionPlan:
    defaults = dict(
        plan_version="v1",
        ticket_id="T-1001",
        category="refund_request",
        actions=[Action(type="issue_refund", params={"amount_usd": 30.0})],
        refund_amount_usd=30.0,
        requires_approval=False,
        confidence=0.9,
        citations=[],
        rationale="test plan",
    )
    defaults.update(overrides)
    return ResolutionPlan(**defaults)


def test_valid_small_refund_passes() -> None:
    assert rules.check_plan(make_plan(), ORDER) == []


# Rule 1: a refund needs a real order and must fit inside its total (v1's
# incident C proposed $249.99 on a $24.99 order).
def test_refund_without_order_data_fails() -> None:
    violations = rules.check_plan(make_plan(), None)
    assert any("requires order_data" in v for v in violations)


def test_refund_above_order_total_fails() -> None:
    plan = make_plan(refund_amount_usd=150.0, requires_approval=True)
    violations = rules.check_plan(plan, ORDER)
    assert any("exceeds order total" in v for v in violations)


def test_refund_without_amount_fails() -> None:
    violations = rules.check_plan(make_plan(refund_amount_usd=None), ORDER)
    assert any("refund_amount_usd" in v for v in violations)


def test_order_data_missing_total_fails() -> None:
    violations = rules.check_plan(make_plan(), {"order_id": "O-48291"})
    assert any("total_usd" in v for v in violations)


# Rule 2: the plan must flag what a person has to sign.
def test_large_refund_requires_approval() -> None:
    plan = make_plan(refund_amount_usd=79.99, requires_approval=False)
    violations = rules.check_plan(plan, ORDER)
    assert any("requires_approval" in v for v in violations)
    assert rules.check_plan(make_plan(refund_amount_usd=79.99, requires_approval=True), ORDER) == []


def test_replacement_requires_approval() -> None:
    plan = make_plan(
        category="replacement",
        actions=[Action(type="send_replacement")],
        refund_amount_usd=None,
        requires_approval=False,
    )
    violations = rules.check_plan(plan, ORDER)
    assert any("requires_approval" in v for v in violations)


# Rule 3: an unsure plan hands off instead of acting.
def test_low_confidence_must_escalate() -> None:
    plan = make_plan(confidence=0.3)
    violations = rules.check_plan(plan, ORDER)
    assert any("escalate_to_human" in v for v in violations)

    escalating = make_plan(
        confidence=0.3,
        actions=[
            Action(type="issue_refund", params={"amount_usd": 30.0}),
            Action(type="escalate_to_human"),
        ],
    )
    assert rules.check_plan(escalating, ORDER) == []


# Rule 4: the actions agree with each other and with the category.
def test_reply_only_cannot_accompany_other_actions() -> None:
    plan = make_plan(
        actions=[
            Action(type="reply_only"),
            Action(type="issue_refund", params={"amount_usd": 30.0}),
        ]
    )
    violations = rules.check_plan(plan, ORDER)
    assert any("reply_only" in v for v in violations)


def test_refund_action_must_match_category() -> None:
    plan = make_plan(category="account")
    violations = rules.check_plan(plan, ORDER)
    assert any("not a valid action for category" in v for v in violations)


# --- ownership and no-silent-cap (added after the laundered $29.99 refund) ---


def test_customer_matches_ignores_case_and_spacing() -> None:
    assert rules.customer_matches("dana  whitfield", "Dana Whitfield") is True
    assert rules.customer_matches("Jordan Whitfield", "Dana Whitfield") is False
    assert rules.customer_matches(None, "Dana Whitfield") is None
    assert rules.customer_matches("Dana Whitfield", None) is None


def test_refund_on_someone_elses_order_fails() -> None:
    violations = rules.check_plan(make_plan(), MISMATCHED_ORDER, customer_name="Dana Whitfield")
    assert any("someone else's order" in v for v in violations)


def test_replacement_on_someone_elses_order_fails() -> None:
    plan = make_plan(
        category="replacement",
        actions=[Action(type="send_replacement")],
        refund_amount_usd=None,
        requires_approval=True,
    )
    violations = rules.check_plan(plan, MISMATCHED_ORDER, customer_name="Dana Whitfield")
    assert any("someone else's order" in v for v in violations)


def test_ownership_is_skipped_when_the_name_is_unknown() -> None:
    # Skipped, not passed: approval_reasons() sends an unknown sender to a
    # person instead (tests/test_laundering.py).
    assert rules.check_plan(make_plan(), MISMATCHED_ORDER) == []  # free-text ticket
    assert rules.check_plan(make_plan(), ORDER, customer_name="Dana Whitfield") == []


def test_request_above_order_total_must_escalate_not_cap() -> None:
    capped = make_plan(requested_refund_usd=10_000.0, refund_amount_usd=79.99, requires_approval=True)
    violations = rules.check_plan(capped, ORDER)
    assert any("do not reduce the amount" in v for v in violations)

    escalated = make_plan(
        requested_refund_usd=10_000.0,
        refund_amount_usd=None,
        actions=[Action(type="escalate_to_human")],
    )
    assert rules.check_plan(escalated, ORDER) == []


def test_refund_that_differs_from_the_request_needs_approval() -> None:
    negotiated = make_plan(requested_refund_usd=45.0, refund_amount_usd=30.0, requires_approval=False)
    violations = rules.check_plan(negotiated, ORDER)
    assert any("differs from what the customer asked for" in v for v in violations)

    signed = make_plan(requested_refund_usd=45.0, refund_amount_usd=30.0, requires_approval=True)
    assert rules.check_plan(signed, ORDER) == []
    exact = make_plan(requested_refund_usd=30.0, refund_amount_usd=30.0)
    assert rules.check_plan(exact, ORDER) == []
