"""llm.py — the only file that talks to the model provider.

Everything else in the checkpoint imports three functions and one model from
here: `structured_completion`, `text_completion`, `stream_text`, `Usage`.
Swapping providers (or adding routing, caching, fallbacks) touches this file
only — which is the whole point of keeping a provider boundary.

NEW in this checkpoint: the boundary is built on **LangChain** instead of the
raw provider SDKs. Checkpoint 01 called `openai` and `anthropic` directly and
needed an `if PROVIDER == "anthropic"` branch inside every function: system
messages move to a different field, structured outputs use a different call,
streaming has a different shape. LangChain's `BaseChatModel` makes the three
providers one interface, so all of that branching collapses into
`_build_chat_model` — one function, the only provider-specific code left.

What LangChain gives you here:
- one interface for every provider: `invoke`, `stream`, `with_structured_output`;
- provider-neutral messages, and `usage_metadata` (token counts) on every reply.

What it does NOT give you, so this file still has to:
- the price table and the cost of each call (`Usage.cost_usd`);
- the error taxonomy — provider exceptions still bubble up through LangChain,
  and someone has to map them onto `errors.*` codes;
- the policy for "malformed output": truncation, refusal, and parse failure
  are three different signals that all have to become LLM_MALFORMED_OUTPUT.

`LLM_PROVIDER` in the repo `.env` picks the provider:

- "azure"     -> Azure OpenAI (`AzureChatOpenAI`). The Azure quirk worth
                 knowing: `model=` is a DEPLOYMENT name you chose in Azure AI
                 Foundry, not a model name (AZURE_OPENAI_DEPLOYMENT).
                 Structured outputs need api-version 2024-08-01-preview or
                 newer on a structured-output-capable model.
- "openai"    -> api.openai.com (`ChatOpenAI`) with OPENAI_API_KEY / OPENAI_MODEL.
- "anthropic" -> the Claude API (`ChatAnthropic`) with ANTHROPIC_API_KEY /
                 ANTHROPIC_MODEL. Current Claude models reject sampling
                 parameters, so `temperature` is never sent; thinking tokens
                 count toward `max_tokens`, hence the floor below.
- "gemini", "openrouter", "ollama", "lmstudio" -> `ChatOpenAI` pointed at
                 another server's OpenAI-compatible API (OPENAI_COMPATIBLE
                 below; setup notes in LLM_PROVIDERS.md at the repo root).

- structured_completion: machine output. `with_structured_output(..., method=
  "json_schema")` uses each provider's NATIVE structured-output mode (OpenAI
  response_format json_schema with strict=True; Anthropic output_config
  format), and `include_raw=True` keeps the raw reply so token usage and the
  finish reason stay visible.
- text_completion: human-facing prose (reply drafts, notes).
- stream_text: like text_completion but yields deltas as they arrive.
"""
from __future__ import annotations

import os
from typing import Callable, Iterator, TypeVar

import anthropic
import openai
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, convert_to_messages
from langchain_openai import AzureChatOpenAI, ChatOpenAI
from pydantic import BaseModel

import errors

load_dotenv()

# Providers that speak OpenAI's API at a different address. `ChatOpenAI`
# talks to all of them: only the base URL, the key and the model name change.
# Ollama and LM Studio run the model on your own machine, so they need no key
# (key_var None). Each base URL can be overridden in .env.
OPENAI_COMPATIBLE: dict[str, dict[str, str | None]] = {
    "gemini": {
        "base_url_var": "GEMINI_BASE_URL",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_var": "GEMINI_API_KEY",
        "model_var": "GEMINI_MODEL",
        "model": "gemini-3.5-flash-lite",
    },
    "openrouter": {
        "base_url_var": "OPENROUTER_BASE_URL",
        "base_url": "https://openrouter.ai/api/v1",
        "key_var": "OPENROUTER_API_KEY",
        "model_var": "OPENROUTER_MODEL",
        "model": "openrouter/free",
    },
    "ollama": {
        "base_url_var": "OLLAMA_BASE_URL",
        "base_url": "http://localhost:11434/v1",
        "key_var": None,
        "model_var": "OLLAMA_MODEL",
        "model": "llama3.2",
    },
    "lmstudio": {
        "base_url_var": "LMSTUDIO_BASE_URL",
        "base_url": "http://localhost:1234/v1",
        "key_var": None,
        "model_var": "LMSTUDIO_MODEL",
        "model": "google/gemma-3-4b",
    },
}
# The two that run on your own machine: no bill per token.
LOCAL_PROVIDERS = {"ollama", "lmstudio"}

PROVIDER = (os.getenv("LLM_PROVIDER") or "azure").strip().lower()
# Fail at import on an unknown name. Every branch below falls through to
# OpenAI, so a typo like "azrue" would otherwise call a different provider.
if PROVIDER not in {"azure", "openai", "anthropic", *OPENAI_COMPATIBLE}:
    raise RuntimeError(
        f"LLM_PROVIDER={PROVIDER!r} is not supported — use 'azure', 'openai', "
        "'anthropic', 'gemini', 'openrouter', 'ollama' or 'lmstudio' "
        "(see .env.example)"
    )

# Resolved as plain names at import time — never raise here, because the tests
# import this module with no credentials at all. Missing keys surface in
# chat_model(), on the first real call.
if PROVIDER == "azure":
    DEFAULT_MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")
elif PROVIDER == "anthropic":
    DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
elif PROVIDER in OPENAI_COMPATIBLE:
    _spec = OPENAI_COMPATIBLE[PROVIDER]
    DEFAULT_MODEL = os.getenv(_spec["model_var"], _spec["model"])
else:
    DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Anthropic only. Thinking tokens count toward max_tokens on current Claude
# models, so a 1024-token cap meant for the answer alone would truncate the
# reply; give it a floor. ANTHROPIC_EFFORT (low|medium|high|xhigh|max) sets how
# hard the model thinks — leave it unset on Haiku 4.5, which rejects it.
ANTHROPIC_MIN_MAX_TOKENS = 16000
ANTHROPIC_EFFORT = (os.getenv("ANTHROPIC_EFFORT") or "").strip().lower() or None

# USD per 1M tokens. Illustrative teaching numbers — check your provider's
# pricing page and edit. Keys are model FAMILIES: on Azure the model string is
# a deployment name ("deskhand-gpt-4.1-mini"), so we match the family inside it.
# Worked example: 1,000 prompt + 100 completion tokens on gpt-4o-mini cost
# (1000 * 0.15 + 100 * 0.60) / 1M = $0.00021.
PRICE_TABLE: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},  # prompts up to 100K tokens
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},  # paid tier; the free tier is $0
    # OpenRouter names models "vendor/model", so "openai/gpt-4o-mini" finds
    # the gpt-4o-mini row above. Its free models cost nothing.
    "openrouter/free": {"input": 0.0, "output": 0.0},
    ":free": {"input": 0.0, "output": 0.0},  # "meta-llama/llama-3.3-70b-instruct:free"
}
# Used when a deployment name matches no family above, so cost tracking stays
# honest-ish instead of reporting $0.00 for every call.
FALLBACK_PRICE = {"input": 0.15, "output": 0.60}
# What a model on LOCAL_PROVIDERS costs, whatever its name.
FREE = {"input": 0.0, "output": 0.0}

# OpenRouter forwards each request to one of several hosts that serve the
# model, and not every host supports structured outputs. require_parameters
# tells it to pick only hosts that honour every parameter we send.
OPENROUTER_ROUTING = {"provider": {"require_parameters": True}}


class Usage(BaseModel):
    """Tokens and dollars for one LLM call. Callers add `cost_usd` up per
    ticket, so every run can report what it spent."""

    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


def _required(name: str) -> str:
    """An env var that must be set. A value still reading "your-..." is the
    .env.example placeholder, so it counts as unset."""
    value = (os.getenv(name) or "").strip()
    if not value or value.startswith("your-"):
        raise RuntimeError(
            f"{name} is not set — fill it in the repo .env "
            f"(LLM_PROVIDER={PROVIDER}, see .env.example)"
        )
    return value


def _compatible_args(provider: str) -> dict[str, str]:
    """base_url and api_key for one of the OPENAI_COMPATIBLE providers."""
    spec = OPENAI_COMPATIBLE[provider]
    return {
        "base_url": os.getenv(spec["base_url_var"]) or spec["base_url"],
        # The SDK refuses to start without some key. Local servers ignore it.
        "api_key": _required(spec["key_var"]) if spec["key_var"] else "not-needed",
    }


# ---------------------------------------------------------------------------
# The chat model: the only provider-specific code in the checkpoint
# ---------------------------------------------------------------------------

def _build_chat_model(
    model: str, temperature: float, max_output_tokens: int
) -> BaseChatModel:
    """Construct the LangChain chat model for the active provider.

    Everything above this function is provider-neutral; this function is the
    constructors. Compare with checkpoint 01, where the same knowledge was
    spread across every function in llm.py.
    """
    # max_retries stays at its default (2) in every one, so the SDK retries
    # 429s, 5xx and timeouts with backoff on its own. A rate-limit error that
    # reaches _guarded below has already failed three times.
    if PROVIDER == "azure":
        return AzureChatOpenAI(
            azure_deployment=model,  # a deployment name, not a model name
            azure_endpoint=_required("AZURE_OPENAI_ENDPOINT"),
            api_key=_required("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
            temperature=temperature,
            max_tokens=max_output_tokens,
            stream_usage=True,  # the last streamed chunk carries token counts
        )
    if PROVIDER == "anthropic":
        effort = {"effort": ANTHROPIC_EFFORT} if ANTHROPIC_EFFORT else {}
        # No stream_usage flag: ChatAnthropic reports streamed usage by default.
        return ChatAnthropic(
            model=model,
            api_key=_required("ANTHROPIC_API_KEY"),
            # No temperature: current Claude models reject sampling params.
            max_tokens=max(max_output_tokens, ANTHROPIC_MIN_MAX_TOKENS),
            **effort,
        )
    if PROVIDER in OPENAI_COMPATIBLE:
        # "Compatible" is not "identical". ChatOpenAI sends the token cap as
        # max_completion_tokens, OpenAI's newer name, and Ollama ignores that
        # field without a word: no cap at all. Every compatible server reads
        # the older max_tokens, so the cap goes into the request body under
        # that name instead.
        body = {"max_tokens": max_output_tokens}
        if PROVIDER == "openrouter":
            body.update(OPENROUTER_ROUTING)
        return ChatOpenAI(
            model=model,
            **_compatible_args(PROVIDER),
            temperature=temperature,
            extra_body=body,
            stream_usage=True,
        )
    return ChatOpenAI(
        model=model,
        api_key=_required("OPENAI_API_KEY"),
        temperature=temperature,
        max_tokens=max_output_tokens,
        stream_usage=True,
    )


# The three parameters are fixed when a model is built, so each combination
# gets its own instance, built once and reused for the life of the process.
_models: dict[tuple[str, float, int], BaseChatModel] = {}


def chat_model(
    model: str | None = None, temperature: float = 0.0, max_output_tokens: int = 1024
) -> BaseChatModel:
    """The configured chat model, built lazily and cached per parameter set.

    Lazy so that importing this module never needs credentials (the offline
    tests rely on that). Public so a lab can do
    `llm.chat_model().bind_tools(nodes.TOOLS)` and see what an agent would look
    like — Deskhand itself never lets the model choose a tool.
    """
    key = (model or DEFAULT_MODEL, temperature, max_output_tokens)
    if key not in _models:
        _models[key] = _build_chat_model(*key)
    return _models[key]


# ---------------------------------------------------------------------------
# Pricing — LangChain reports tokens; dollars are still our job
# ---------------------------------------------------------------------------

def _prices(model: str) -> dict[str, float]:
    if PROVIDER in LOCAL_PROVIDERS:
        return FREE  # "llama3.2" would otherwise get FALLBACK_PRICE
    exact = PRICE_TABLE.get(model)
    if exact is not None:
        return exact
    name = model.lower()
    # Longest family match wins, so "gpt-4o-mini" beats "gpt-4o".
    families = [family for family in PRICE_TABLE if family in name]
    if families:
        return PRICE_TABLE[max(families, key=len)]
    return FALLBACK_PRICE


def _cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    prices = _prices(model)
    return (
        prompt_tokens * prices["input"] + completion_tokens * prices["output"]
    ) / 1_000_000


def _usage_from(model: str, message: AIMessage) -> Usage:
    """Token counts come from LangChain's provider-neutral `usage_metadata`."""
    meta = message.usage_metadata or {}
    prompt = int(meta.get("input_tokens", 0))
    completion = int(meta.get("output_tokens", 0))
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=_cost_usd(model, prompt, completion),
    )


# ---------------------------------------------------------------------------
# Error taxonomy — provider exceptions still arrive, LangChain just relays them
# ---------------------------------------------------------------------------

# Both SDKs' classes, since either provider can be active. LangChain passes
# provider errors through as the SDK's classes (or subclasses of them), so
# these isinstance checks still match. Connection failures count as timeouts:
# both mean no answer came back, and neither says anything about the prompt.
_RATE_LIMITED = (openai.RateLimitError, anthropic.RateLimitError)
_TRANSIENT = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
)

T = TypeVar("T")


def _is_content_filter(exc: Exception) -> bool:
    """Azure OpenAI rejects a PROMPT its own filter reads as a jailbreak with
    a 400 whose error code is content_filter (T-1018 triggers it at triage).
    That is the provider's input guard firing before ours could, so it maps
    onto the same code as ours: the ticket goes to a human, the run does not
    crash with an unmapped BadRequestError."""
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if code is None and isinstance(body, dict):
        code = body.get("code") or (body.get("error") or {}).get("code")
    return code == "content_filter" or "content_filter" in str(exc)


def _guarded(call: Callable[[], T]) -> T:
    """Run one provider call, translating SDK exceptions into taxonomy codes."""
    try:
        return call()
    except _RATE_LIMITED as exc:
        raise errors.DeskhandError(errors.LLM_RATE_LIMITED, str(exc)) from exc
    except _TRANSIENT as exc:
        raise errors.DeskhandError(errors.LLM_TIMEOUT, str(exc)) from exc
    except Exception as exc:
        if _is_content_filter(exc):
            raise errors.DeskhandError(
                errors.GUARDRAIL_INJECTION_SUSPECTED,
                f"the provider's content filter rejected the prompt: {str(exc)[:200]}",
            ) from exc
        # Anything else (a bad key, a bad request) leaves llm.py unmapped;
        # the graph runner records it as INTERNAL_UNEXPECTED.
        raise


def _stop_reason(message: AIMessage) -> str | None:
    """OpenAI calls it finish_reason, Anthropic stop_reason. Same question."""
    meta = message.response_metadata or {}
    return meta.get("finish_reason") or meta.get("stop_reason")


def _structured_kwargs() -> dict:
    """OpenAI-family models take strict=True so the schema is enforced
    server-side. ChatAnthropic's native mode is strict by construction and
    has no such flag."""
    return {} if PROVIDER == "anthropic" else {"strict": True}


# ---------------------------------------------------------------------------
# The three calls everyone else uses
# ---------------------------------------------------------------------------

def structured_completion(
    messages: list[dict],
    response_model: type[BaseModel],
    model: str | None = None,
    temperature: float = 0.0,  # low randomness for labels; v1 used 1.0 (incident #8)
    max_output_tokens: int = 1024,
) -> tuple[BaseModel, Usage]:
    """One LLM call that must return a valid `response_model` instance.

    `include_raw=True` makes LangChain hand back {"raw", "parsed",
    "parsing_error"} instead of just the parsed object. We need the raw
    message for token usage and the finish reason, and `parsing_error` to say
    why nothing parsed (a refusal and a schema miss look the same otherwise).
    """
    model = model or DEFAULT_MODEL
    # json_schema is the provider's native structured-output mode: the Pydantic
    # model goes over as a JSON schema and generation is held to it. Strict
    # mode needs every object's fields listed up front, so an untyped `dict`
    # field is rejected by the API (see ActionParams in models.py).
    structured = chat_model(model, temperature, max_output_tokens).with_structured_output(
        response_model, method="json_schema", include_raw=True, **_structured_kwargs()
    )
    # convert_to_messages turns the role/content dicts callers pass into
    # LangChain messages. The lambda delays the call so it runs inside
    # _guarded's try.
    result = _guarded(lambda: structured.invoke(convert_to_messages(messages)))

    raw: AIMessage = result["raw"]
    usage = _usage_from(model, raw)
    stop = _stop_reason(raw)
    # Check why the model stopped before looking at the parse: a reply cut off
    # at the token cap is a different failure from a schema miss. v1 never
    # asked (Incident A).
    if stop in {"length", "max_tokens"}:
        raise errors.DeskhandError(
            errors.LLM_MALFORMED_OUTPUT,
            f"output truncated: hit the token cap ({stop})",
        )
    parsed = result.get("parsed")
    # isinstance, not "is None": a dict or any other stray shape is rejected
    # too, so callers only ever get the typed model.
    if not isinstance(parsed, response_model):
        detail = result.get("parsing_error") or (
            "refused" if stop == "refusal" else "no parsed output"
        )
        raise errors.DeskhandError(
            errors.LLM_MALFORMED_OUTPUT,
            f"model did not return valid {response_model.__name__}: {detail}",
        )
    return parsed, usage


def text_completion(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.0,
    max_output_tokens: int = 1024,
) -> tuple[str, Usage]:
    """One LLM call returning plain prose (for reply drafts, notes)."""
    model = model or DEFAULT_MODEL
    chat = chat_model(model, temperature, max_output_tokens)
    message = _guarded(lambda: chat.invoke(convert_to_messages(messages)))
    # `.text` joins only the text blocks. On Claude, thinking blocks are also
    # part of `.content` and must never reach a customer.
    return message.text, _usage_from(model, message)


# Usage of the most recent stream_text call (generators can't also return a
# Usage without complicating the calling code). Read it right after draining
# the iterator.
last_stream_usage: Usage | None = None


def stream_text(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.0,
    max_output_tokens: int = 1024,
) -> Iterator[str]:
    """Stream a prose completion, yielding text deltas as they arrive.

    Token counts arrive differently per provider. OpenAI and Azure send them
    once, on the last chunk; Anthropic sends input on the first and output on
    the last; Gemini repeats a running total on every chunk. The largest
    count seen on any chunk is right in all three cases, where adding the
    chunks up would count Gemini's tokens many times over.
    """
    global last_stream_usage
    model = model or DEFAULT_MODEL
    last_stream_usage = None
    chat = chat_model(model, temperature, max_output_tokens)
    prompt = completion = 0
    # Not wrapped in _guarded: a generator raises while the caller iterates,
    # after any wrapper around the call would have returned. So the mapping
    # sits around the loop, and covers rate limits and connection failures.
    try:
        for chunk in chat.stream(convert_to_messages(messages)):
            meta = chunk.usage_metadata or {}
            prompt = max(prompt, int(meta.get("input_tokens", 0)))
            completion = max(completion, int(meta.get("output_tokens", 0)))
            if chunk.text:
                yield chunk.text
    except _RATE_LIMITED as exc:
        raise errors.DeskhandError(errors.LLM_RATE_LIMITED, str(exc)) from exc
    except _TRANSIENT as exc:
        raise errors.DeskhandError(errors.LLM_TIMEOUT, str(exc)) from exc
    # Runs only once the stream is drained. An exception, or a caller that
    # stops early, leaves it None.
    last_stream_usage = Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=_cost_usd(model, prompt, completion),
    )
