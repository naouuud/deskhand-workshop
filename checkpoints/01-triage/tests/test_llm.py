"""Offline tests for the provider boundary in llm.py.

The provider client is replaced with a stub that raises: no network, no API
key, no cost. The point: whatever the SDK throws, what leaves llm.py is a
DeskhandError with a taxonomy code, never a raw provider exception.

A stub is a small fake object with the same shape as the real one. Here it
has the same attribute path as the real SDK client, but its method raises
the error we want to test instead of calling the internet.
"""

import anthropic
import httpx
import httpx2
import openai
import pytest

import errors
import llm
from models import TriageResult

MESSAGES = [{"role": "system", "content": "triage"}, {"role": "user", "content": "hi"}]


# The SDK error classes need an HTTP response to be constructed, so we build a
# minimal fake one. No request is sent; example.invalid is a reserved name.
def _openai_error(cls, status):
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://example.invalid/v1")
    )
    return cls("boom", response=response, body=None)


# The anthropic SDK here is built on httpx2, so its errors take httpx2 objects.
def _anthropic_error(cls, status):
    response = httpx2.Response(
        status, request=httpx2.Request("POST", "https://example.invalid/v1")
    )
    return cls("boom", response=response, body=None)


class _RaisingOpenAI:
    """Mirrors client.beta.chat.completions.parse, then raises."""

    # The nested classes only exist to recreate that dotted path, so that
    # llm.py's real call line works unchanged against the stub.
    def __init__(self, exc):
        exc_ = exc

        class _Completions:
            def parse(self, **kwargs):
                raise exc_

        class _Chat:
            completions = _Completions()

        class _Beta:
            chat = _Chat()

        self.beta = _Beta()


class _RaisingAnthropic:
    """Mirrors client.messages.parse, then raises."""

    def __init__(self, exc):
        exc_ = exc

        class _Messages:
            def parse(self, **kwargs):
                raise exc_

        self.messages = _Messages()


# Swapping `_client` is the whole offline trick: llm.py looks the name up on
# every call, so the stub wins over the lru_cache'd real client. A 401 or 400
# is no one's to retry, so it maps to INTERNAL_UNEXPECTED, never a raw error.
# parametrize runs the same test once per row: (error class, HTTP status,
# the taxonomy code we expect out of llm.py).
@pytest.mark.parametrize(
    "cls, status, code",
    [
        (openai.RateLimitError, 429, errors.LLM_RATE_LIMITED),
        (openai.AuthenticationError, 401, errors.INTERNAL_UNEXPECTED),  # bad key
        (
            openai.BadRequestError,
            400,
            errors.INTERNAL_UNEXPECTED,
        ),  # e.g. Azure content filter
    ],
)
def test_openai_errors_become_taxonomy_codes(monkeypatch, cls, status, code):
    monkeypatch.setattr(llm, "PROVIDER", "azure")
    monkeypatch.setattr(
        llm, "_client", lambda: _RaisingOpenAI(_openai_error(cls, status))
    )
    # pytest.raises passes only if the block raises this exception type;
    # `info.value` is the exception, so we can check its code.
    with pytest.raises(errors.DeskhandError) as info:
        llm.structured_completion(MESSAGES, TriageResult)
    assert info.value.internal_error_code == code


@pytest.mark.parametrize(
    "cls, status, code",
    [
        (anthropic.RateLimitError, 429, errors.LLM_RATE_LIMITED),
        (anthropic.AuthenticationError, 401, errors.INTERNAL_UNEXPECTED),
        (anthropic.BadRequestError, 400, errors.INTERNAL_UNEXPECTED),
    ],
)
def test_anthropic_errors_become_taxonomy_codes(monkeypatch, cls, status, code):
    monkeypatch.setattr(llm, "PROVIDER", "anthropic")
    monkeypatch.setattr(
        llm, "_client", lambda: _RaisingAnthropic(_anthropic_error(cls, status))
    )
    with pytest.raises(errors.DeskhandError) as info:
        llm.structured_completion(MESSAGES, TriageResult)
    assert info.value.internal_error_code == code


def test_haiku_5_5_is_priced():
    # The lab prices checkpoint 00's calls with this table too: a missing row
    # silently reports $0.
    # 1M tokens in at $0.10 plus 1M tokens out at $0.50 = $0.60. pytest.approx
    # compares floats with a tiny tolerance, the safe habit for float maths.
    assert llm._cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(
        0.60
    )


# --- the OpenAI-compatible providers ----------------------------------------


@pytest.mark.parametrize(
    "provider, base_url",
    [
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("ollama", "http://localhost:11434/v1"),
        ("lmstudio", "http://localhost:1234/v1"),
    ],
)
def test_compatible_providers_are_the_openai_client_at_another_address(
    monkeypatch, provider, base_url
):
    # Building a client sends nothing, so this stays offline.
    monkeypatch.setattr(llm, "PROVIDER", provider)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv(f"{provider.upper()}_BASE_URL", raising=False)
    llm._client.cache_clear()
    try:
        client = llm._client()
    finally:
        llm._client.cache_clear()  # don't leave a stub-env client for later tests
    assert type(client) is openai.OpenAI
    assert str(client.base_url).rstrip("/") == base_url.rstrip("/")


def test_local_providers_need_no_key_but_cloud_ones_do(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert llm._compatible_args("ollama")["api_key"]  # a placeholder the server ignores
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is not set"):
        llm._compatible_args("gemini")


def test_base_url_can_be_overridden(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://192.168.1.20:11434/v1")
    assert llm._compatible_args("ollama")["base_url"] == "http://192.168.1.20:11434/v1"


def test_openrouter_and_local_models_are_priced():
    # OpenRouter's "vendor/model" names find the family row; free models and
    # models on your own laptop cost nothing.
    assert llm._cost_usd("openai/gpt-4o-mini", 1000, 100) == llm._cost_usd(
        "gpt-4o-mini", 1000, 100
    )
    assert llm._cost_usd("meta-llama/llama-3.3-70b-instruct:free", 1000, 100) == 0
    assert llm._cost_usd("llama3.2", 1000, 100) == 0
    assert llm._cost_usd(
        "gemini-3.5-flash-lite", 1_000_000, 1_000_000
    ) == pytest.approx(2.80)
