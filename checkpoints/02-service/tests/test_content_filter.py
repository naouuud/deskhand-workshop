"""A provider's content filter rejecting the PROMPT is the provider's input
guard firing first. It maps onto GUARDRAIL_INJECTION_SUSPECTED, not onto an
unmapped 400 that crashes the run (Azure does this to T-1018 at triage).
"""
from __future__ import annotations

import pytest

import errors
import llm


class _AzureStyleError(Exception):
    """Shaped like openai.BadRequestError: a .code and a .body."""

    def __init__(self) -> None:
        super().__init__(
            "Error code: 400 - {'error': {'message': 'The response was filtered "
            "due to the prompt triggering Azure OpenAI's content management "
            "policy.', 'code': 'content_filter', 'status': 400}}"
        )
        self.code = "content_filter"
        self.body = {"error": {"code": "content_filter"}}


def test_content_filter_rejection_is_the_guard_code() -> None:
    def call():
        raise _AzureStyleError()

    with pytest.raises(errors.DeskhandError) as exc_info:
        llm._guarded(call)
    assert exc_info.value.internal_error_code == errors.GUARDRAIL_INJECTION_SUSPECTED
    assert "content filter" in str(exc_info.value)


def test_content_filter_is_recognised_from_the_message_alone() -> None:
    # Fallback for an exception without .code or .body: the text is matched.
    def call():
        raise RuntimeError("400 {'error': {'code': 'content_filter'}}")

    with pytest.raises(errors.DeskhandError) as exc_info:
        llm._guarded(call)
    assert exc_info.value.internal_error_code == errors.GUARDRAIL_INJECTION_SUSPECTED


def test_other_provider_errors_still_surface_unmapped() -> None:
    # Only the content filter is remapped. Anything else keeps its own type,
    # so a real bug is never disguised as an injection attempt.
    def call():
        raise ValueError("something else entirely")

    with pytest.raises(ValueError):
        llm._guarded(call)
