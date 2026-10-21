"""llm.py, the only module that talks to the LLM provider.

A provider is the company whose servers run the model (OpenAI, Azure,
Anthropic). Each one ships a Python SDK, a client library that wraps its HTTP
API. Every function here takes a list of chat messages, calls the provider's
SDK and returns plain Python values. Nothing outside this file imports
`openai` or `anthropic`.

Swapping providers (or mocking in tests) touches this file alone. Checkpoint 01
only uses structured_completion; text_completion (reply drafts) and stream_text
(streaming replies) are part of the same contract and come into play from
checkpoint 02 on.

`LLM_PROVIDER` in the repo `.env` picks the provider:

- "azure"     -> Azure OpenAI. The Azure quirk worth knowing: `model=` is a
                 DEPLOYMENT name you chose in Azure AI Foundry, not a model
                 name (AZURE_OPENAI_DEPLOYMENT). Structured outputs need
                 api-version 2024-08-01-preview or newer on a
                 structured-output-capable model (gpt-4.1, gpt-4o-2024-08-06+).
- "openai"    -> api.openai.com with OPENAI_API_KEY / OPENAI_MODEL.
- "anthropic" -> the Claude API with ANTHROPIC_API_KEY / ANTHROPIC_MODEL.
- "gemini", "openrouter", "ollama", "lmstudio" -> OpenAI-compatible servers
                 (see OPENAI_COMPATIBLE below and LLM_PROVIDERS.md at the
                 repo root). Ollama and LM Studio run on your own machine.

Azure, OpenAI and the four OpenAI-compatible providers share an SDK and the
calls below; only the client construction and the meaning of `model` differ.
Anthropic is its own SDK, so
each function has an `if PROVIDER == "anthropic"` branch: system messages move
to the top-level `system` field and structured outputs use
`client.messages.parse`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache
from typing import TYPE_CHECKING

import openai
import pydantic
from dotenv import load_dotenv
from pydantic import BaseModel

import errors

# The anthropic SDK is imported inside the Anthropic branches below, not here.
# Importing an SDK loads hundreds of files (several seconds on some laptops),
# and an Azure or OpenAI run never uses it. This import is for type hints only.
if TYPE_CHECKING:
    import anthropic

# Copies KEY=value lines from the nearest .env file into os.environ, so the
# os.getenv calls below can see them. Keys stay out of the code and out of git.
load_dotenv()

# Providers that speak OpenAI's API at a different address. The same `openai`
# client talks to all of them: only the base URL, the key and the model name
# change. Ollama and LM Studio run the model on your own machine, so they need
# no key (key_var None). Each base URL can be overridden in .env.
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

PROVIDER = (os.getenv("LLM_PROVIDER") or "azure").strip().lower()
# Fail at import on an unknown name. Every branch below falls through to
# OpenAI, so a typo like "azrue" would otherwise call a different provider.
if PROVIDER not in {"azure", "openai", "anthropic", *OPENAI_COMPATIBLE}:
    raise RuntimeError(
        f"LLM_PROVIDER={PROVIDER!r} is not supported — use 'azure', 'openai', "
        "'anthropic', 'gemini', 'openrouter', 'ollama' or 'lmstudio' "
        "(see .env.example)"
    )

# Names only — reading them never fails, so importing this module (as the tests
# do) never needs credentials. Missing keys surface on the first real call.
if PROVIDER == "azure":
    DEFAULT_MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")
elif PROVIDER == "anthropic":
    DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
elif PROVIDER in OPENAI_COMPATIBLE:
    _spec = OPENAI_COMPATIBLE[PROVIDER]
    DEFAULT_MODEL = os.getenv(_spec["model_var"], _spec["model"])
else:
    DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Providers bill by the token. A token is a chunk of text, roughly three
# quarters of an English word. Input (prompt) tokens and output (completion)
# tokens have different prices, and output is usually the expensive side.
# USD per 1M tokens. Illustrative — check the provider's pricing page and edit.
# Keys are model FAMILIES: on Azure the model string is a deployment name
# ("deskhand-gpt-4.1-mini"), so we look for the family inside it.
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
    "gemini-3.5-flash-lite": {
        "input": 0.30,
        "output": 2.50,
    },  # paid tier; the free tier is $0
    # OpenRouter names models "vendor/model", so "openai/gpt-4o-mini" finds
    # the gpt-4o-mini row above. Its free models cost nothing.
    "openrouter/free": {"input": 0.0, "output": 0.0},
    ":free": {"input": 0.0, "output": 0.0},  # "meta-llama/llama-3.3-70b-instruct:free"
}
# Ollama and LM Studio models ("llama3.2") match no row, and an unknown model
# prices at 0 below: right for a model running on your own laptop.

# OpenRouter forwards each request to one of several hosts that serve the
# model, and not every host supports structured outputs. require_parameters
# tells it to pick only hosts that honour every parameter we send.
OPENROUTER_ROUTING = {"provider": {"require_parameters": True}}

# Anthropic only. Current Claude models can "think" (reason step by step)
# before answering, and those thinking tokens count toward max_tokens, so a
# 1024-token cap meant for the answer alone would truncate it; give it a
# floor. ANTHROPIC_EFFORT (low|medium|high|xhigh|max) sets how hard
# the model thinks; leave it unset on Haiku 4.5, which rejects it.
ANTHROPIC_MIN_MAX_TOKENS = 16000
ANTHROPIC_EFFORT = (os.getenv("ANTHROPIC_EFFORT") or "").strip().lower() or None


class Usage(BaseModel):
    """Tokens and dollars for one LLM call. Callers add `cost_usd` up per
    ticket, so every run can report what it spent."""

    prompt_tokens: int  # tokens we sent: system prompt + ticket (+ repair turn)
    completion_tokens: int  # tokens the model wrote back
    cost_usd: float


def _cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    # Exact name first ("gpt-4o-mini"), then a family found inside a longer
    # name ("deskhand-gpt-4o-mini" contains "gpt-4o-mini").
    prices = PRICE_TABLE.get(model)
    if prices is None:
        families = [family for family in PRICE_TABLE if family in model.lower()]
        if not families:  # unknown deployment: track tokens, price 0 rather than guess
            return 0.0
        prices = PRICE_TABLE[max(families, key=len)]  # "gpt-4o-mini" beats "gpt-4o"
    return (
        prompt_tokens * prices["input"] + completion_tokens * prices["output"]
    ) / 1_000_000


def _usage(response_usage, model: str) -> Usage:  # noqa: ANN001 - openai or anthropic usage
    # Same counts, different field names: Anthropic says input/output,
    # OpenAI says prompt/completion.
    if PROVIDER == "anthropic":
        prompt, completion = response_usage.input_tokens, response_usage.output_tokens
    else:
        prompt, completion = (
            response_usage.prompt_tokens,
            response_usage.completion_tokens,
        )
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=_cost_usd(model, prompt, completion),
    )


def _require(name: str) -> str:
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
        "api_key": _require(spec["key_var"]) if spec["key_var"] else "not-needed",
    }


# lru_cache remembers a function's return value. With no arguments and
# maxsize=1, the first call builds the client and every later call gets that
# same object back, so we connect once instead of once per ticket.
@lru_cache(maxsize=1)
def _client() -> openai.OpenAI | openai.AzureOpenAI | anthropic.Anthropic:
    # Created lazily so importing this module never requires an API key.
    # lru_cache builds it once per process. The SDK clients retry 429s, 5xx
    # and timeouts twice with backoff by default (max_retries=2), so an error
    # that reaches the except blocks below has already failed three times.
    # (429 is the HTTP status for "rate limited"; 5xx means a server error.)
    if PROVIDER == "anthropic":
        import anthropic

        return anthropic.Anthropic(api_key=_require("ANTHROPIC_API_KEY"))
    if PROVIDER == "azure":
        return openai.AzureOpenAI(
            api_key=_require("AZURE_OPENAI_API_KEY"),
            azure_endpoint=_require("AZURE_OPENAI_ENDPOINT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        )
    if PROVIDER in OPENAI_COMPATIBLE:
        # The plain OpenAI client, pointed at another server.
        return openai.OpenAI(**_compatible_args(PROVIDER))
    return openai.OpenAI(api_key=_require("OPENAI_API_KEY"))


def _anthropic_kwargs(messages: list[dict], model: str, max_output_tokens: int) -> dict:
    """OpenAI-style messages -> Messages API arguments. No `temperature`:
    current Claude models reject sampling params.

    The rest of the code always builds OpenAI-style message lists. This
    function is the adapter: Anthropic wants the system text in its own
    `system` argument instead of as a message with role "system"."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    kwargs: dict = {
        "model": model,
        "max_tokens": max(max_output_tokens, ANTHROPIC_MIN_MAX_TOKENS),
        "messages": [m for m in messages if m["role"] != "system"],
    }
    if system:
        kwargs["system"] = system
    if ANTHROPIC_EFFORT:
        kwargs["output_config"] = {"effort": ANTHROPIC_EFFORT}
    return kwargs


def structured_completion(
    messages: list[dict],
    response_model: type[BaseModel],
    model: str | None = None,
    # Temperature controls randomness: 0 picks the most likely words every
    # time, higher values make answers more varied. Labels should not vary.
    temperature: float = 0.0,  # low randomness for labels; v1 used 1.0 (incident #8)
    max_output_tokens: int = 1024,  # hard cap on reply length (and so on cost)
) -> tuple[BaseModel, Usage]:
    """One LLM call that must return a valid `response_model` instance.

    Uses the provider's structured-output mode (json_schema). Provider
    exceptions never escape this module; they're mapped to taxonomy codes:
    rate limits and timeouts get their own codes, every other provider error
    (a bad key, a bad request, a content filter) is INTERNAL_UNEXPECTED.

    Structured outputs, in one line: instead of asking "please reply in JSON"
    and hoping, we send the provider the exact schema and it only produces
    replies that match it.
    """
    model = model or DEFAULT_MODEL
    if PROVIDER == "anthropic":
        return _anthropic_structured(messages, response_model, model, max_output_tokens)
    # parse() sends the Pydantic model as a strict JSON schema
    # (response_format json_schema, strict: true). The provider then generates
    # only replies that fit it, and the SDK validates the reply back into the
    # model. Strict mode needs every object's fields listed up front, so an
    # untyped `dict` field is rejected by the API.
    try:
        response = _client().beta.chat.completions.parse(
            model=model,
            messages=messages,
            response_format=response_model,
            temperature=temperature,
            max_tokens=max_output_tokens,
            extra_body=OPENROUTER_ROUTING if PROVIDER == "openrouter" else None,
        )
    # Typed error mapping. Order matters: RateLimitError and APITimeoutError
    # are subclasses of APIError, so they must come before the catch-all.
    # `detail` keeps the raw provider text for logs; users only ever see
    # errors.user_message(). LengthFinishReasonError is v1's Incident A: the
    # reply hit max_tokens and the JSON was cut off. Here it gets a name.
    # `raise ... from exc` keeps the original exception attached, so a
    # traceback still shows what the SDK really said.
    except openai.LengthFinishReasonError as exc:
        raise errors.DeskhandError(
            errors.LLM_MALFORMED_OUTPUT, f"output truncated: {exc}"
        ) from exc
    except openai.RateLimitError as exc:
        raise errors.DeskhandError(errors.LLM_RATE_LIMITED, str(exc)) from exc
    except openai.APITimeoutError as exc:
        raise errors.DeskhandError(errors.LLM_TIMEOUT, str(exc)) from exc
    except openai.APIError as exc:  # auth, bad request, content filter, connection
        raise errors.DeskhandError(
            errors.INTERNAL_UNEXPECTED, f"{type(exc).__name__}: {exc}"
        ) from exc
    # The API can return several alternative replies ("choices"); we asked
    # for one, so we read the first.
    message = response.choices[0].message
    if message.parsed is None:  # model refused, or output failed schema parsing
        raise errors.DeskhandError(
            errors.LLM_MALFORMED_OUTPUT, message.refusal or "no parsed output"
        )
    # message.parsed is already an instance of response_model (a TriageResult
    # here), not a dict and not a JSON string.
    return message.parsed, _usage(response.usage, model)


def _anthropic_structured(
    messages: list[dict],
    response_model: type[BaseModel],
    model: str,
    max_output_tokens: int,
) -> tuple[BaseModel, Usage]:
    """The Anthropic version of the call above. `messages.parse` sends the
    Pydantic model as the output format and validates the reply into
    `parsed_output`; the stop_reason checks name truncation and refusals."""
    import anthropic

    try:
        response = _client().messages.parse(
            **_anthropic_kwargs(messages, model, max_output_tokens),
            output_format=response_model,
        )
    except anthropic.RateLimitError as exc:
        raise errors.DeskhandError(errors.LLM_RATE_LIMITED, str(exc)) from exc
    except anthropic.APIConnectionError as exc:  # includes APITimeoutError
        raise errors.DeskhandError(errors.LLM_TIMEOUT, str(exc)) from exc
    except anthropic.APIError as exc:  # auth, bad request, overloaded
        raise errors.DeskhandError(
            errors.INTERNAL_UNEXPECTED, f"{type(exc).__name__}: {exc}"
        ) from exc
    except pydantic.ValidationError as exc:
        raise errors.DeskhandError(errors.LLM_MALFORMED_OUTPUT, str(exc)) from exc
    # stop_reason says why the model stopped writing. "end_turn" is the
    # normal finish; the two below mean the reply cannot be trusted.
    if response.stop_reason == "max_tokens":
        raise errors.DeskhandError(
            errors.LLM_MALFORMED_OUTPUT, "output truncated: hit max_tokens"
        )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        detail = "refused" if response.stop_reason == "refusal" else "no parsed output"
        raise errors.DeskhandError(errors.LLM_MALFORMED_OUTPUT, detail)
    return response.parsed_output, _usage(response.usage, model)


def text_completion(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.0,
    max_output_tokens: int = 1024,
) -> tuple[str, Usage]:
    """Plain-text completion, for customer reply drafts, never machine output.

    "Machine output" means anything code will read and act on (a category, a
    refund amount). That always goes through structured_completion. Free text
    is only for humans to read."""
    model = model or DEFAULT_MODEL
    if PROVIDER == "anthropic":
        import anthropic

        try:
            response = _client().messages.create(
                **_anthropic_kwargs(messages, model, max_output_tokens)
            )
        except anthropic.RateLimitError as exc:
            raise errors.DeskhandError(errors.LLM_RATE_LIMITED, str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise errors.DeskhandError(errors.LLM_TIMEOUT, str(exc)) from exc
        except anthropic.APIError as exc:
            raise errors.DeskhandError(
                errors.INTERNAL_UNEXPECTED, f"{type(exc).__name__}: {exc}"
            ) from exc
        # Text blocks only. With thinking on, `content` also holds thinking
        # blocks, and those must never reach a customer.
        text = "".join(block.text for block in response.content if block.type == "text")
        return text, _usage(response.usage, model)
    try:
        response = _client().chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_output_tokens,
        )
    except openai.RateLimitError as exc:
        raise errors.DeskhandError(errors.LLM_RATE_LIMITED, str(exc)) from exc
    except openai.APITimeoutError as exc:
        raise errors.DeskhandError(errors.LLM_TIMEOUT, str(exc)) from exc
    except openai.APIError as exc:  # auth, bad request, content filter, connection
        raise errors.DeskhandError(
            errors.INTERNAL_UNEXPECTED, f"{type(exc).__name__}: {exc}"
        ) from exc
    # `content` can be None (a refusal, for one); callers always get a str.
    return response.choices[0].message.content or "", _usage(response.usage, model)


def stream_text(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.0,
    max_output_tokens: int = 1024,
) -> Iterator[str]:
    """Yield text deltas as they arrive. Only ever used for the customer reply
    (checkpoint 02+); machine output must never stream partially.

    Streaming: the provider sends the reply in small pieces ("deltas") as the
    model writes it, so a person sees words appear instead of waiting for the
    whole answer. This is a generator: `yield` hands each piece to the caller's
    for loop as soon as it arrives.

    No Usage comes back: this version neither asks for stream usage nor
    returns it, so a streamed call goes unpriced. Checkpoint 02 adds both."""
    model = model or DEFAULT_MODEL
    if PROVIDER == "anthropic":
        import anthropic

        # The try wraps the whole loop: a stream can fail halfway, while the
        # caller is already iterating, not only when it opens.
        try:
            with _client().messages.stream(
                **_anthropic_kwargs(messages, model, max_output_tokens)
            ) as stream:
                yield from stream.text_stream
        except anthropic.RateLimitError as exc:
            raise errors.DeskhandError(errors.LLM_RATE_LIMITED, str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise errors.DeskhandError(errors.LLM_TIMEOUT, str(exc)) from exc
        except anthropic.APIError as exc:
            raise errors.DeskhandError(
                errors.INTERNAL_UNEXPECTED, f"{type(exc).__name__}: {exc}"
            ) from exc
        return
    try:
        stream = _client().chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_output_tokens,
            stream=True,
        )
        # Some chunks carry no text: Azure's first chunk has empty `choices`
        # (prompt-filter results only), and the last one only says why the
        # model stopped. v1 hit the empty one too (00-broken-bot/bot.py).
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except openai.RateLimitError as exc:
        raise errors.DeskhandError(errors.LLM_RATE_LIMITED, str(exc)) from exc
    except openai.APITimeoutError as exc:
        raise errors.DeskhandError(errors.LLM_TIMEOUT, str(exc)) from exc
    except openai.APIError as exc:  # auth, bad request, content filter, connection
        raise errors.DeskhandError(
            errors.INTERNAL_UNEXPECTED, f"{type(exc).__name__}: {exc}"
        ) from exc
