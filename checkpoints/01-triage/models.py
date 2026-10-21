"""Canonical Pydantic models for checkpoint 01.

An LLM replies with text. Your code needs data: a category it can compare, a
number it can check against 0.5. This file is where we say exactly what shape
that data must have.

Pydantic is a library that turns a Python class into a data contract. You
declare fields with types, and Pydantic checks real data against them: a
missing field, a wrong type or a value outside the allowed list raises an
error instead of slipping through.

v1 (00-broken-bot) ran json.loads on whatever text came back and hoped. Here
the Pydantic model IS the contract: llm.structured_completion hands it to the
provider as the output schema (a JSON description of the fields and their
allowed values), and a reply that does not fit fails at that one boundary,
not three functions later.
"""

from typing import Literal

from pydantic import BaseModel, Field


class TriageResult(BaseModel):
    """The triage step's structured output: one ticket's classification.

    Inheriting from BaseModel is what makes this a Pydantic model. Each
    annotated attribute below becomes a required field.

    category, priority and sentiment are closed Literal sets: Literal["a", "b"]
    means "exactly one of these strings, nothing else". The JSON schema sent
    to the provider lists the allowed values, so the model cannot answer
    "billing" or "very urgent", and Pydantic re-checks every value on parse.
    """

    category: Literal[
        "refund_request",
        "replacement",
        "shipping_delay",
        "product_defect",
        "account",
        "other",  # a safe bucket, so the model never has to force a bad fit
    ]
    priority: Literal["low", "medium", "high", "urgent"]
    sentiment: Literal["calm", "frustrated", "angry"]
    # How sure the model is of its category. It is the model's own guess, not
    # a measured probability; from 02-service on, low-confidence plans go to
    # a human.
    confidence: (
        float  # 0..1 — the model's self-estimate; business rules act on it later
    )
