"""Business-rule validation for resolution plans.

Schema validation (Pydantic) proves the plan is well-formed JSON of the right
shape. These rules prove it is *allowed*: a plan can be perfectly valid JSON
and still try to refund more than the order total.

In this checkpoint the thresholds are hardcoded constants; checkpoint 03 keeps
the same rules but justifies them with retrieved policy text.
"""
from __future__ import annotations

from models import ResolutionPlan

# Why plain code and not the model: the planner PROPOSES a plan, these rules
# DISPOSE of it. The same constraints are written into PLAN_SYSTEM_PROMPT
# (nodes.py), but a prompt is a request the model may ignore, and a ticket can
# argue with it (prompt injection: instructions hidden in customer text). An
# if-statement gives the same answer every run, cannot be talked out of it, and
# is unit-tested offline (tests/test_rules.py).
#
# Each violation string has two readers: a person in the logs, and the model,
# because repair_plan sends the list back as the repair re-prompt. That is why
# several of them say what the plan must do instead.

AUTO_APPROVE_LIMIT_USD = 50.0  # above this a person signs; the plan prompt repeats "$50.00", change both
# The model's own estimate of its confidence, not a measured probability. Low
# confidence may only route to a human; high confidence never unlocks anything.
CONFIDENCE_FLOOR = 0.5


def customer_matches(ticket_name: str | None, order_name: str | None) -> bool | None:
    """Does the ticket's customer own the order? None when either side is unknown
    (a free-text ticket submitted without customer_name). Case and spacing
    are ignored; anything else is a mismatch, and a mismatch is a human's
    call. None is not a pass: approval_reasons() sends it to a person."""
    if not ticket_name or not order_name:
        return None

    # No fuzzy matching on purpose: "Dana Whitfeld" is a mismatch, and a
    # person decides whether it is a typo.
    def norm(name: str) -> str:
        return " ".join(name.casefold().split())

    return norm(ticket_name) == norm(order_name)

# Which categories may carry which "hard" actions (rule 4 consistency check).
# plan.category is written by the planner itself, not copied from triage, so
# rule 4 checks the plan against its own label.
_ACTION_ALLOWED_CATEGORIES: dict[str, frozenset[str]] = {
    "issue_refund": frozenset({"refund_request", "product_defect"}),
    "send_replacement": frozenset({"replacement", "product_defect"}),
}


def check_plan(
    plan: ResolutionPlan,
    order_data: dict | None,
    *,
    customer_name: str | None = None,
) -> list[str]:
    """Return a list of human-readable rule violations (empty = plan is valid).

    Every rule runs, so one repair re-prompt sees all the problems at once.
    `order_data` is what the order API returned (None when no order was
    found), never what the model claims. `customer_name` is who the ticket is
    from, as the dataset or the client vouched for it.
    """
    violations: list[str] = []
    action_types = [a.type for a in plan.actions]

    # Rule 1: a refund needs real order data and must not exceed the order total.
    # This is v1's incident C: the customer said the $24.99 band cost $249.99,
    # and v1 proposed a $249.99 refund. The elif chain reports the first gap only.
    if "issue_refund" in action_types:
        if order_data is None:
            violations.append(
                "issue_refund requires order_data, but no order was found "
                "for this ticket"
            )
        elif plan.refund_amount_usd is None:
            violations.append("issue_refund requires refund_amount_usd to be set")
        elif "total_usd" not in order_data:
            violations.append(
                "order_data has no total_usd — cannot verify the refund amount"
            )
        elif plan.refund_amount_usd > float(order_data["total_usd"]):
            violations.append(
                f"refund_amount_usd ({plan.refund_amount_usd}) exceeds order "
                f"total ({order_data['total_usd']})"
            )

    # Rule 1, continued: the order must be the CUSTOMER'S OWN. A mismatched
    # identity is an escalation trigger (escalation-matrix), never a write.
    # `is False`, not `not`: None (nobody vouched for the sender) is not a
    # violation here. approval_reasons() sends it to a person instead.
    if order_data is not None and {"issue_refund", "send_replacement"} & set(action_types):
        if customer_matches(customer_name, order_data.get("customer_name")) is False:
            violations.append(
                f"the ticket is from {customer_name!r} but order "
                f"{order_data.get('order_id')} belongs to "
                f"{order_data.get('customer_name')!r} — no refund or replacement "
                "on someone else's order; use request_more_info or escalate_to_human"
            )

    # Rule 1, continued: a request ABOVE the order total is an escalation, not
    # a cap. The model must not quietly shrink what was asked for into an
    # amount that passes (the $10,000-becomes-$29.99 incident).
    # That plan was "laundered": the model turned an illegitimate ask into an
    # action that looked legitimate, and every amount rule passed. The rules
    # checked the number the model chose, never the number the customer asked
    # for. requested_refund_usd is model-written too, so this rule catches a
    # model that reports the ask honestly and caps anyway (what happened). The
    # ownership check above does not depend on the model at all.
    if (
        "issue_refund" in action_types
        and order_data is not None
        and "total_usd" in order_data
        and plan.requested_refund_usd is not None
        and plan.requested_refund_usd > float(order_data["total_usd"])
    ):
        violations.append(
            f"the customer asked for ${plan.requested_refund_usd:.2f}, more than the "
            f"order total (${float(order_data['total_usd']):.2f}) — the actions must "
            "be escalate_to_human; do not reduce the amount and refund"
        )

    # Rule 2: large refunds and replacements need a human in the loop.
    # A violation, not only a gate: a plan that says "no approval" for $79.99
    # misread the policy, so it goes back for repair. approval_reasons() below
    # decides the gate itself and does not trust this flag.
    needs_approval = (
        plan.refund_amount_usd is not None
        and plan.refund_amount_usd > AUTO_APPROVE_LIMIT_USD
    ) or "send_replacement" in action_types
    if needs_approval and not plan.requires_approval:
        violations.append(
            f"refunds above ${AUTO_APPROVE_LIMIT_USD:.2f} and replacements "
            "require requires_approval=true"
        )

    # Rule 2, continued: giving a different amount than was asked for is a
    # negotiation, and a person signs negotiations.
    if (
        plan.refund_amount_usd is not None
        and plan.requested_refund_usd is not None
        # Compare cents: float noise (29.990000000000002) is not a negotiation.
        and round(plan.refund_amount_usd, 2) != round(plan.requested_refund_usd, 2)
        and not plan.requires_approval
    ):
        violations.append(
            f"refund_amount_usd ({plan.refund_amount_usd}) differs from what the "
            f"customer asked for ({plan.requested_refund_usd}) — requires_approval=true"
        )

    # Rule 3: a low-confidence plan must hand off to a human.
    if plan.confidence < CONFIDENCE_FLOOR and "escalate_to_human" not in action_types:
        violations.append(
            f"confidence {plan.confidence} is below {CONFIDENCE_FLOOR} — "
            "the plan must include escalate_to_human"
        )

    # Rule 4: the action set must be internally and categorically consistent.
    # reply_only means "nothing to execute", so pairing it with a write
    # contradicts itself.
    if "reply_only" in action_types and len(action_types) > 1:
        violations.append("reply_only cannot be combined with other actions")
    for action_type, allowed in _ACTION_ALLOWED_CATEGORIES.items():
        if action_type in action_types and plan.category not in allowed:
            violations.append(
                f"{action_type} is not a valid action for category "
                f"'{plan.category}' (allowed: {sorted(allowed)})"
            )

    return violations


# The text the approver sees when nobody vouched for the sender (free-text
# tickets submitted without a customer_name). A named constant so the tests
# can assert the exact text the approver sees.
UNKNOWN_SENDER_REASON = (
    "sender unknown: nobody vouched that this customer owns the order"
)


def approval_reasons(plan: ResolutionPlan, customer_match: bool | None) -> list[str]:
    """Why a person must sign this (valid) plan before anything is written.

    This, not the model's requires_approval flag, decides whether the run
    stops at the approval gate: empty = auto-execute. The model's flag can only
    add a stop, never remove one. Rule 2 already makes the model SET the flag
    for big refunds and replacements; the unknown sender is decided here alone.
    It complements rule 1's ownership check: a known stranger (customer_match
    False) is refused outright, an unknown sender (None) goes to a person,
    whatever the amount."""
    # validate_plan calls this only for a plan with no violations, and stops
    # at the approval gate when the list is non-empty. Each reason is shown to
    # the approver, so it names a cause, not only "needs approval".
    reasons: list[str] = []
    action_types = {a.type for a in plan.actions}
    if plan.refund_amount_usd is not None and plan.refund_amount_usd > AUTO_APPROVE_LIMIT_USD:
        reasons.append(
            f"refund ${plan.refund_amount_usd:.2f} is above the "
            f"${AUTO_APPROVE_LIMIT_USD:.2f} auto-approve limit"
        )
    if "send_replacement" in action_types:
        reasons.append("replacements always need a person")
    if (
        plan.refund_amount_usd is not None
        and plan.requested_refund_usd is not None
        and round(plan.refund_amount_usd, 2) != round(plan.requested_refund_usd, 2)
    ):
        reasons.append(
            f"refund ${plan.refund_amount_usd:.2f} differs from the "
            f"${plan.requested_refund_usd:.2f} the customer asked for"
        )
    if {"issue_refund", "send_replacement"} & action_types and customer_match is None:
        reasons.append(UNKNOWN_SENDER_REASON)
    # The model's flag can add a stop (with a generic reason) but never remove one.
    if plan.requires_approval and not reasons:
        reasons.append("the planner asked for a person to review this plan")
    return reasons
