"""Error taxonomy for Deskhand.

Every failure in the system carries one of these internal error codes. The code
is for engineers (logs, dashboards, debugging); the user-visible message is
ALWAYS chosen separately from USER_MESSAGES — never str(exception).
"""
from __future__ import annotations

# Canonical taxonomy — keep in sync with the workshop spec.
# A code says what the CALLER should do, which is why there are several and
# not one "LLM_ERROR". v1 retried every failure the same way, then shrugged.

# Model calls. Only malformed output (truncated, refused, off-schema) is worth
# a repair re-prompt. Rate limits and timeouts already got the SDK's own
# retries; re-prompting cannot fix them, so callers let them propagate.
LLM_MALFORMED_OUTPUT = "LLM_MALFORMED_OUTPUT"
LLM_RATE_LIMITED = "LLM_RATE_LIMITED"
LLM_TIMEOUT = "LLM_TIMEOUT"
# Order API (checkpoint 02 on), raised after the tool's own retries. NOT_FOUND
# is an answer, not a failure: it is logged and the plan has no order data.
TOOL_ORDER_API_TIMEOUT = "TOOL_ORDER_API_TIMEOUT"
TOOL_ORDER_API_ERROR = "TOOL_ORDER_API_ERROR"
TOOL_ORDER_NOT_FOUND = "TOOL_ORDER_NOT_FOUND"
# Validation. A broken business rule (02 on) is logged and sent back for
# repair; REPAIR_EXHAUSTED ends the run when the bounded repair loop runs out.
# Nothing raises VALIDATION_SCHEMA_FAILED: a schema miss arrives as
# LLM_MALFORMED_OUTPUT from the model call.
VALIDATION_SCHEMA_FAILED = "VALIDATION_SCHEMA_FAILED"
VALIDATION_BUSINESS_RULE_FAILED = "VALIDATION_BUSINESS_RULE_FAILED"
REPAIR_EXHAUSTED = "REPAIR_EXHAUSTED"
# Deliberate stops: the run ends and a human takes the ticket. BUDGET_EXCEEDED
# is agent_bounded.py's step cap in 02 and budget.py's cost cap in 04. The
# injection guard is the provider's content filter from 02, plus Deskhand's
# own input guards in 04. Neither is retried: the same input trips it again.
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
GUARDRAIL_INJECTION_SUSPECTED = "GUARDRAIL_INJECTION_SUSPECTED"
# The SSE client went away (02 on). Logged, never raised: the run carries on.
STREAM_DISCONNECTED = "STREAM_DISCONNECTED"
# Anything without a code of its own: a bug, a bad API key. The safe default.
INTERNAL_UNEXPECTED = "INTERNAL_UNEXPECTED"


class DeskhandError(Exception):
    """A typed, taxonomy-carrying failure.

    `message` is internal (safe to log, never shown to customers).
    """

    def __init__(self, internal_error_code: str, message: str) -> None:
        # str(exc) is this internal message: fine for logs and for a repair
        # re-prompt to the model, never for a customer.
        super().__init__(message)
        self.internal_error_code = internal_error_code


# Safe, user-visible messages per error code. Deliberately boring and honest.
# Codes share a message when the customer cannot act on the difference: a
# timeout and a 5xx from the order API read the same to them. Every message
# says what happens next. A code missing here falls back to _GENERIC.
_GENERIC = (
    "Something went wrong while processing this ticket. "
    "It has been routed to a human agent."
)

USER_MESSAGES: dict[str, str] = {
    REPAIR_EXHAUSTED: (
        "We couldn't generate a reliable resolution for this ticket. "
        "It has been routed to a human agent."
    ),
    TOOL_ORDER_API_TIMEOUT: (
        "We couldn't look up the order for this ticket right now. "
        "It has been routed to a human agent."
    ),
    TOOL_ORDER_API_ERROR: (
        "We couldn't look up the order for this ticket right now. "
        "It has been routed to a human agent."
    ),
    LLM_RATE_LIMITED: _GENERIC,
    LLM_TIMEOUT: _GENERIC,
    LLM_MALFORMED_OUTPUT: _GENERIC,
    BUDGET_EXCEEDED: (
        "This ticket needs more attention than we can automate. "
        "It has been routed to a human agent."
    ),
    # Says nothing about what was detected: telling an attacker which
    # phrasing tripped the guard helps them write the next attempt.
    GUARDRAIL_INJECTION_SUSPECTED: (
        "This ticket has been routed to a human agent for review."
    ),
    INTERNAL_UNEXPECTED: _GENERIC,
}


def user_message_for(internal_error_code: str | None) -> str:
    """Pick the safe user-visible message for an error code.

    handle_failure calls this for every failed run. An unknown code or None
    still gets a safe message, never an empty one.
    """
    return USER_MESSAGES.get(internal_error_code or "", _GENERIC)
