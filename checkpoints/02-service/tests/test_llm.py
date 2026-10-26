"""The provider boundary, offline.

A stub stands in for the LangChain chat model, so these tests prove llm.py's
POLICY without a network or a key: what counts as malformed output, how token
usage becomes dollars, how SDK exceptions map onto the taxonomy. LangChain's
own behaviour is not under test here; ours is.
"""
from __future__ import annotations

import anthropic
import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

import errors
import llm
from models import TriageResult

GOOD = TriageResult(
    category="refund_request", priority="medium", sentiment="calm", confidence=0.9
)
USER = [{"role": "user", "content": "x"}]


def _raw(prompt: int = 1000, completion: int = 100, **metadata) -> AIMessage:
    """A provider reply as LangChain shapes it. llm.py reads only its
    usage_metadata (tokens) and response_metadata (why it stopped)."""
    return AIMessage(
        content="",
        usage_metadata={
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": prompt + completion,
        },
        response_metadata=metadata or {"finish_reason": "stop"},
    )


class StubStructured:
    """Stands in for `with_structured_output(...)`: invoke returns the
    include_raw dict {"raw", "parsed", "parsing_error"}, or raises."""

    def __init__(self, result: dict | None = None, exc: Exception | None = None):
        self.result, self.exc = result, exc

    def invoke(self, messages):
        if self.exc:
            raise self.exc
        return self.result


class StubChat:
    """Just enough of BaseChatModel for llm.py: with_structured_output, invoke, stream."""

    def __init__(self, structured=None, message=None, chunks=None, exc=None):
        self.structured, self.message, self.chunks, self.exc = (
            structured, message, chunks, exc
        )
        self.schema = None
        self.structured_kwargs: dict = {}

    def with_structured_output(self, schema, **kwargs):
        self.schema, self.structured_kwargs = schema, kwargs
        return self.structured

    def invoke(self, messages):
        if self.exc:
            raise self.exc
        return self.message

    def stream(self, messages):
        if self.exc:
            raise self.exc
        yield from self.chunks


@pytest.fixture()
def use_stub(monkeypatch):
    """Install a StubChat as the configured model. Returns an installer."""
    holder: dict = {}

    def fake_chat_model(model=None, temperature=0.0, max_output_tokens=1024):
        holder["params"] = (model, temperature, max_output_tokens)
        return holder["chat"]

    # chat_model is the one seam: patch it and no real model is ever built,
    # so no key and no network.
    monkeypatch.setattr(llm, "chat_model", fake_chat_model)

    def install(chat: StubChat) -> dict:
        holder["chat"] = chat
        return holder

    return install


def _rate_limit(sdk):
    response = httpx.Response(429, request=httpx.Request("POST", "https://x"))
    return sdk.RateLimitError("slow down", response=response, body=None)


def _timeout(sdk):
    return sdk.APITimeoutError(request=httpx.Request("POST", "https://x"))


# --- structured_completion ---------------------------------------------------

def test_structured_happy_path_returns_parsed_and_cost(use_stub) -> None:
    chat = StubChat(StubStructured({"raw": _raw(), "parsed": GOOD, "parsing_error": None}))
    holder = use_stub(chat)

    parsed, usage = llm.structured_completion(USER, TriageResult, model="gpt-4o-mini")

    assert parsed == GOOD
    assert (usage.prompt_tokens, usage.completion_tokens) == (1000, 100)
    assert usage.cost_usd == pytest.approx(llm._cost_usd("gpt-4o-mini", 1000, 100))
    assert chat.schema is TriageResult
    assert chat.structured_kwargs["method"] == "json_schema"
    assert chat.structured_kwargs["include_raw"] is True
    assert holder["params"] == ("gpt-4o-mini", 0.0, 1024)


def test_openai_family_asks_for_strict_schema(use_stub, monkeypatch) -> None:
    chat = StubChat(StubStructured({"raw": _raw(), "parsed": GOOD, "parsing_error": None}))
    use_stub(chat)
    monkeypatch.setattr(llm, "PROVIDER", "azure")
    llm.structured_completion(USER, TriageResult)
    assert chat.structured_kwargs["strict"] is True

    monkeypatch.setattr(llm, "PROVIDER", "anthropic")
    llm.structured_completion(USER, TriageResult)
    assert "strict" not in chat.structured_kwargs


@pytest.mark.parametrize(
    "metadata",
    [{"finish_reason": "length"}, {"stop_reason": "max_tokens"}],
    ids=["openai-length", "anthropic-max_tokens"],
)
def test_truncation_is_malformed_output(use_stub, metadata) -> None:
    use_stub(StubChat(StubStructured(
        {"raw": _raw(**metadata), "parsed": None, "parsing_error": ValueError("eof")}
    )))
    with pytest.raises(errors.DeskhandError) as info:
        llm.structured_completion(USER, TriageResult)
    assert info.value.internal_error_code == errors.LLM_MALFORMED_OUTPUT
    assert "truncated" in str(info.value)


def test_refusal_or_parse_failure_is_malformed_output(use_stub) -> None:
    use_stub(StubChat(StubStructured(
        {"raw": _raw(), "parsed": None, "parsing_error": Exception("I can't help with that")}
    )))
    with pytest.raises(errors.DeskhandError) as info:
        llm.structured_completion(USER, TriageResult)
    assert info.value.internal_error_code == errors.LLM_MALFORMED_OUTPUT
    assert "TriageResult" in str(info.value)
    assert "can't help" in str(info.value)


def test_wrong_parsed_type_never_leaks_out(use_stub) -> None:
    use_stub(StubChat(StubStructured(
        {"raw": _raw(), "parsed": {"category": "other"}, "parsing_error": None}
    )))
    with pytest.raises(errors.DeskhandError) as info:
        llm.structured_completion(USER, TriageResult)
    assert info.value.internal_error_code == errors.LLM_MALFORMED_OUTPUT


@pytest.mark.parametrize("sdk", [openai, anthropic], ids=["openai", "anthropic"])
def test_rate_limit_maps_to_taxonomy_for_both_sdks(use_stub, sdk) -> None:
    use_stub(StubChat(StubStructured(exc=_rate_limit(sdk))))
    with pytest.raises(errors.DeskhandError) as info:
        llm.structured_completion(USER, TriageResult)
    assert info.value.internal_error_code == errors.LLM_RATE_LIMITED


@pytest.mark.parametrize("sdk", [openai, anthropic], ids=["openai", "anthropic"])
def test_timeout_maps_to_taxonomy_for_both_sdks(use_stub, sdk) -> None:
    # text_completion goes through the same _guarded as structured_completion.
    use_stub(StubChat(exc=_timeout(sdk)))
    with pytest.raises(errors.DeskhandError) as info:
        llm.text_completion(USER)
    assert info.value.internal_error_code == errors.LLM_TIMEOUT


# --- text_completion / stream_text -------------------------------------------

def test_text_completion_returns_text_and_usage(use_stub) -> None:
    message = AIMessage(
        content="Hello there",
        usage_metadata={"input_tokens": 50, "output_tokens": 5, "total_tokens": 55},
    )
    use_stub(StubChat(message=message))
    text, usage = llm.text_completion(USER, model="gpt-4o")
    assert text == "Hello there"
    assert usage.prompt_tokens == 50
    assert usage.cost_usd == pytest.approx(llm._cost_usd("gpt-4o", 50, 5))


def test_text_completion_drops_thinking_blocks(use_stub) -> None:
    """Claude returns thinking blocks in .content; the customer must never see them."""
    message = AIMessage(content=[
        {"type": "thinking", "thinking": "internal reasoning", "signature": "s"},
        {"type": "text", "text": "Your refund is approved."},
    ])
    use_stub(StubChat(message=message))
    text, _ = llm.text_completion(USER)
    assert text == "Your refund is approved."


def test_stream_text_yields_deltas_and_sums_usage_across_chunks(use_stub) -> None:
    # Anthropic-style: input tokens on the first chunk, output on the last.
    chunks = [
        AIMessageChunk(content="Hel", usage_metadata={
            "input_tokens": 40, "output_tokens": 0, "total_tokens": 40}),
        AIMessageChunk(content="lo"),
        AIMessageChunk(content="", usage_metadata={
            "input_tokens": 0, "output_tokens": 7, "total_tokens": 7}),
    ]
    use_stub(StubChat(chunks=chunks))

    deltas = list(llm.stream_text(USER, temperature=0.4))

    assert deltas == ["Hel", "lo"]  # the usage-only chunk is not yielded
    assert llm.last_stream_usage is not None
    assert (llm.last_stream_usage.prompt_tokens,
            llm.last_stream_usage.completion_tokens) == (40, 7)


def test_stream_text_rate_limit_maps_to_taxonomy(use_stub) -> None:
    # stream_text has its own mapping: a generator can't go through _guarded.
    use_stub(StubChat(exc=_rate_limit(openai)))
    with pytest.raises(errors.DeskhandError) as info:
        list(llm.stream_text(USER))
    assert info.value.internal_error_code == errors.LLM_RATE_LIMITED


# --- the provider-specific corner ----------------------------------------------

def test_chat_model_is_cached_per_parameter_set(monkeypatch) -> None:
    builds: list[tuple] = []
    monkeypatch.setattr(llm, "_models", {})
    monkeypatch.setattr(
        llm, "_build_chat_model", lambda *key: builds.append(key) or object()
    )
    a = llm.chat_model("m", 0.0, 1024)
    b = llm.chat_model("m", 0.0, 1024)
    c = llm.chat_model("m", 0.4, 1024)
    assert a is b and a is not c
    assert builds == [("m", 0.0, 1024), ("m", 0.4, 1024)]


def test_missing_credentials_fail_on_first_call_not_at_import(monkeypatch) -> None:
    monkeypatch.setattr(llm, "_models", {})
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
        llm.chat_model("gpt-4o-mini")


def test_build_chat_model_per_provider(monkeypatch) -> None:
    """The three constructors: deployment vs model name, and Claude's quirks."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    monkeypatch.setattr(llm, "PROVIDER", "azure")
    azure = llm._build_chat_model("my-deployment", 0.0, 1024)
    assert type(azure).__name__ == "AzureChatOpenAI"
    assert azure.deployment_name == "my-deployment"
    assert (azure.temperature, azure.max_tokens) == (0.0, 1024)

    monkeypatch.setattr(llm, "PROVIDER", "openai")
    oai = llm._build_chat_model("gpt-4o-mini", 0.4, 512)
    assert type(oai).__name__ == "ChatOpenAI"
    assert (oai.model_name, oai.temperature, oai.max_tokens) == ("gpt-4o-mini", 0.4, 512)

    monkeypatch.setattr(llm, "PROVIDER", "anthropic")
    claude = llm._build_chat_model("claude-opus-5-5", 0.4, 1024)
    assert type(claude).__name__ == "ChatAnthropic"
    assert claude.temperature is None  # sampling params are rejected by the API
    assert claude.max_tokens == llm.ANTHROPIC_MIN_MAX_TOKENS  # thinking floor


# --- the OpenAI-compatible providers -------------------------------------------

def test_stream_text_reads_a_running_total_once(use_stub) -> None:
    # Gemini-style: every chunk repeats the running total. Adding the chunks
    # up would report 3x the prompt; the largest count is the real one.
    chunks = [
        AIMessageChunk(content="Hel", usage_metadata={
            "input_tokens": 28, "output_tokens": 2, "total_tokens": 30}),
        AIMessageChunk(content="lo", usage_metadata={
            "input_tokens": 28, "output_tokens": 18, "total_tokens": 46}),
        AIMessageChunk(content="", usage_metadata={
            "input_tokens": 28, "output_tokens": 18, "total_tokens": 46}),
    ]
    use_stub(StubChat(chunks=chunks))

    assert list(llm.stream_text(USER)) == ["Hel", "lo"]
    assert (llm.last_stream_usage.prompt_tokens,
            llm.last_stream_usage.completion_tokens) == (28, 18)


@pytest.mark.parametrize(
    "provider, base_url",
    [
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("ollama", "http://localhost:11434/v1"),
        ("lmstudio", "http://localhost:1234/v1"),
    ],
)
def test_compatible_providers_are_chat_openai_at_another_address(
    monkeypatch, provider, base_url
) -> None:
    monkeypatch.setattr(llm, "PROVIDER", provider)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv(f"{provider.upper()}_BASE_URL", raising=False)

    chat = llm._build_chat_model("some-model", 0.0, 1024)

    assert type(chat).__name__ == "ChatOpenAI"
    assert chat.openai_api_base == base_url
    assert (chat.model_name, chat.temperature) == ("some-model", 0.0)
    # The cap travels as max_tokens (Ollama ignores max_completion_tokens),
    # and only OpenRouter needs telling to route to hosts that honour the schema.
    assert chat.max_tokens is None
    expected = {"max_tokens": 1024}
    if provider == "openrouter":
        expected.update(llm.OPENROUTER_ROUTING)
    assert chat.extra_body == expected
    payload = chat._get_request_payload(USER)
    assert "max_completion_tokens" not in payload  # extra_body is merged in by the SDK


def test_local_providers_need_no_key_but_cloud_ones_do(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert llm._compatible_args("lmstudio")["api_key"]  # a placeholder the server ignores
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is not set"):
        llm._compatible_args("gemini")


def test_local_models_are_free_and_openrouter_names_find_their_family(monkeypatch) -> None:
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    assert llm._cost_usd("llama3.2", 1000, 100) == 0  # not FALLBACK_PRICE

    monkeypatch.setattr(llm, "PROVIDER", "openrouter")
    assert llm._prices("openai/gpt-4o-mini") == llm.PRICE_TABLE["gpt-4o-mini"]
    assert llm._cost_usd("openrouter/free", 1000, 100) == 0
    assert llm._cost_usd("meta-llama/llama-3.3-70b-instruct:free", 1000, 100) == 0
