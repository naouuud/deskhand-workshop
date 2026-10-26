"""Canonical Pydantic models for the Deskhand workflow.

These shapes are the contract between nodes, the validator, the API, and the
frontend. Machine output (ResolutionPlan) is strictly typed; human-facing text
(customer_reply_draft, internal_note) lives in separate fields and is never
mixed into machine output.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TriageResult(BaseModel):
    """The triage step's structured output: one ticket's classification.

    category, priority and sentiment are closed Literal sets. The JSON schema
    sent to the provider lists the allowed values, so the model cannot answer
    "billing" or "very urgent", and Pydantic re-checks every value on parse.
    """

    category: Literal[
        "refund_request",
        "replacement",
        "shipping_delay",
        "product_defect",
        "account",
        "other",
    ]
    priority: Literal["low", "medium", "high", "urgent"]
    sentiment: Literal["calm", "frustrated", "angry"]
    confidence: float = Field(ge=0.0, le=1.0)  # checked on parse: a 1.3 never reaches the rules


class ActionParams(BaseModel):
    """Arguments for an action, one optional field per action type.

    This is deliberately NOT a bare `dict`. Strict structured outputs reject an
    open-ended object (the provider needs `additionalProperties: false` and a
    declared property list), so an untyped hole here fails the API call outright
    — and an untyped hole is the exact thing this checkpoint is retiring
    everywhere else.
    """

    # extra="forbid": an unknown key ("amount" for "amount_usd") fails
    # validation instead of being dropped silently, and the JSON schema says
    # additionalProperties: false, which strict mode requires.
    model_config = ConfigDict(extra="forbid")

    # execute_actions pays plan.refund_amount_usd, the field the rules check.
    # amount_usd here is not what moves the money.
    amount_usd: float | None = None  # issue_refund
    sku: str | None = None           # send_replacement
    reason: str | None = None        # escalate_to_human / request_more_info


class Action(BaseModel):
    # The closed menu of things a plan may ask for. The model names an action;
    # code decides whether and how it runs (rules.py, then execute_actions).
    type: Literal[
        "issue_refund",
        "send_replacement",
        "escalate_to_human",
        "request_more_info",
        "reply_only",
    ]
    params: ActionParams = Field(default_factory=ActionParams)


class ResolutionPlan(BaseModel):
    """The planner's structured output: the contract between the model and
    the code that checks and executes its plan.

    Schema validation guarantees the SHAPE: the right fields, the right
    types, only allowed Literal values. It cannot say whether the plan is
    ALLOWED (a well-formed plan can still refund more than the order total);
    rules.check_plan does that. Every field the model fills is a proposal.
    """

    plan_version: Literal["v1"]  # a one-value Literal: every stored plan names its contract
    ticket_id: str  # overwritten with state.ticket_id after each call: code owns it
    # The planner writes this one. Nothing checks it against state.triage,
    # and rule 4 checks the actions against it.
    category: str  # mirrors triage category
    actions: list[Action]
    # No default: the model must always state it (null when there is no
    # refund). It is the amount rules 1 and 2 check and execute_actions pays.
    refund_amount_usd: float | None
    # What the customer asked for: the stated amount, the order total for
    # "a full refund", None when no amount or no refund was requested. The
    # rules compare it with refund_amount_usd: a plan may not quietly give
    # less (or more) than was asked without a person signing off.
    requested_refund_usd: float | None = None
    requires_approval: bool  # the model's opinion; rules.approval_reasons decides the gate
    confidence: float = Field(ge=0.0, le=1.0)  # below rules.CONFIDENCE_FLOOR the plan must escalate
    citations: list[str]  # policy section ids like "refund-policy#window"; [] before cp03
    rationale: str  # internal reasoning summary, NOT sent to customer


class TicketResolutionState(BaseModel):
    """Everything one run knows, passed from node to node.

    store.save_state writes it to SQLite as JSON after every node, so a run
    that crashes resumes from its last saved node. It is never sent to the
    model as a response schema, which is why the untyped dicts below are
    fine here and would not be in ResolutionPlan.
    """

    # One id per run: the key in runs.db, in every logs/runs.jsonl line, and
    # in the refund idempotency key (request_id:action_index).
    request_id: str
    ticket_id: str
    ticket_text: str
    # Who the ticket is from, as the rules trust it: the dataset record's name,
    # or customer_name posted with free text. None = nobody vouched for it.
    customer_name: str | None = None
    customer_match: bool | None = None  # set by fetch_order_data; None if unknown
    normalized_ticket: str | None = None
    triage: TriageResult | None = None
    order_data: dict | None = None  # what the order API returned; the rules trust this, not the ticket
    policy_chunks: list[dict] = Field(default_factory=list)  # cp03+
    resolution_plan: ResolutionPlan | None = None
    validation_errors: list[str] = Field(default_factory=list)
    # Bounded repair: after nodes.MAX_REPAIR_ATTEMPTS re-prompts, a plan that
    # is still invalid fails the run with REPAIR_EXHAUSTED.
    repair_attempts: int = 0
    requires_approval: bool = False  # decided by rules.approval_reasons
    approval_reasons: list[str] = Field(default_factory=list)  # shown to the approver
    approval_status: Literal["not_required", "pending", "approved", "rejected"] = (
        "not_required"
    )
    customer_reply_draft: str | None = None
    internal_note: str | None = None
    status: Literal[
        "queued",
        "running",
        "needs_repair",
        "awaiting_approval",
        "executing",
        "failed",
        "completed",
    ] = "queued"
    # Two error fields on purpose. The code is for logs and dashboards; the
    # message comes from errors.user_message_for and is the only one a
    # customer sees. Neither is ever str(exception).
    user_visible_error: str | None = None
    internal_error_code: str | None = None
    # Summed from every LLM call's Usage. It lives on the state, so it is
    # saved with the run and survives a resume.
    cost_usd: float = 0.0
    node_latency_ms: dict[str, int] = Field(default_factory=dict)


# Finished runs. Crash recovery (store.find_resumable) never restarts these,
# and the SSE stream closes once their last event is delivered.
TERMINAL_STATUSES = frozenset({"failed", "completed"})
