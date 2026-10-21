"""Lab runner for checkpoint 01 — runs the real triage CLI on one ticket.

Started by lab/server.py with cwd = checkpoints/01-triage. main.main() runs
unmodified; the only change is main.TICKETS_PATH pointing at a temp file that
holds the one ticket the student picked, so the run goes through the same
code path as `py main.py` (request_id, run.started log line, classify_ticket,
the rich table, user_message on failure).

Observed from the outside, every wrapper calls straight through:
- llm._client       -> proxy that records each provider call (messages sent,
                       raw content, finish_reason, usage) — including calls
                       whose output failed to parse.
- llm.structured_completion -> the DeskhandError each attempt raised.
- triage.classify_ticket    -> request_id, result, reported cost.
- errors.user_message       -> the customer-safe text main.py chose.
- logs/runs.jsonl           -> only the lines this run appended.

Optional fault injection (off unless requested, labelled in every trace):
{"inject": {"truncate_attempts": 1}} sends max_tokens=5 on the first N
provider calls. The provider really truncates, the SDK really raises
LengthFinishReasonError, and triage.py's real repair loop handles it — the
same idea as the checkpoint's own tests, which monkeypatch llm.

Request (stdin): {"mode": "run", "ticket": {...}, "inject": {...}}
             or  {"mode": "quote", "models": ["gpt-4.1-mini"]}   (pricing only)
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

import labproto

# Third-party packages 01's modules import, loaded while the runner waits warm
# (labproto.preload). llm.py imports anthropic only on the Anthropic path, so
# preload_list() adds it only when this runner will call Claude.
PRELOAD = ("openai", "pydantic", "dotenv", "rich.console", "rich.table")


def preload_list() -> tuple[str, ...]:
    """PRELOAD, plus anthropic when LLM_PROVIDER says so: the lab's model
    picker sets it in the env, else it comes from the repo .env (the file
    llm.py's load_dotenv() finds, two folders up from the cwd)."""
    from dotenv import dotenv_values

    provider = os.environ.get("LLM_PROVIDER") or dotenv_values(
        Path.cwd().parents[1] / ".env"
    ).get("LLM_PROVIDER")
    return PRELOAD + (
        ("anthropic",) if (provider or "").strip().lower() == "anthropic" else ()
    )


INJECTED_MAX_TOKENS = (
    5  # too few for any TriageResult: the provider stops with finish_reason "length"
)


def quote(req: dict, ch: labproto.Channel) -> None:
    """USD per 1M tokens, as checkpoint 01's own _cost_usd prices them."""
    import llm

    prices = {
        model: {
            "input": llm._cost_usd(model, 1_000_000, 0),
            "output": llm._cost_usd(model, 0, 1_000_000),
        }
        for model in req.get("models", [])
    }
    ch.emit("quote", prices=prices, file=llm.__file__)


def run(req: dict, ch: labproto.Channel, cap: labproto.CapturedStdout) -> None:
    """{"mode": "run"}: install the wrappers below, run main.main() on one
    ticket, then emit one "trace". The process ends after it, so only
    TICKETS_PATH is put back."""
    started = time.perf_counter()
    ticket = req["ticket"]
    truncate_attempts = int((req.get("inject") or {}).get("truncate_attempts") or 0)

    # The checkpoint's own modules: cwd is checkpoints/01-triage and
    # use_checkpoint_dir() put it first on sys.path.
    import errors
    import llm
    import main as triage_main
    import models
    import obslog
    import triage

    calls: list[dict] = []
    attempts: list[dict] = []
    observed: dict = {
        "request_id": None,
        "result": None,
        "reported_cost_usd": None,
        "error": None,
        "user_message": None,
    }

    # --- provider proxy -------------------------------------------------------
    # llm._client() builds (and caches) the SDK client. Every call in llm.py
    # goes through llm._client(), so replacing that one name puts the proxy
    # in front of every provider call without editing llm.py.
    real_client_factory = llm._client

    class _ParseCompletions:
        def parse(self, **kwargs):
            """Record the call, apply the fault injection, call the real client."""
            call = {
                "n": len(calls) + 1,
                "model": kwargs.get("model"),
                "temperature": kwargs.get("temperature"),
                "max_tokens": kwargs.get("max_tokens"),
                "injected": None,
                "raw": None,
                "refusal": None,
                "finish_reason": None,
                "usage": None,
                "error": None,
            }
            sent = dict(kwargs)
            if call["n"] <= truncate_attempts:
                sent["max_tokens"] = INJECTED_MAX_TOKENS
                call["injected"] = (
                    f"max_tokens={INJECTED_MAX_TOKENS} (lab fault injection)"
                )
            calls.append(call)
            ch.emit("attempt.started", n=call["n"], injected=call["injected"])
            t0 = time.perf_counter()
            try:
                response = real_client_factory().beta.chat.completions.parse(**sent)
            except BaseException as exc:
                # Record what came back even on failure: a truncated reply is the evidence.
                completion = getattr(
                    exc, "completion", None
                )  # LengthFinishReasonError carries it
                if completion is not None:
                    choice = completion.choices[0]
                    call["raw"] = choice.message.content
                    call["finish_reason"] = choice.finish_reason
                    call["usage"] = labproto.usage_dict(completion.usage)
                call["error"] = f"{type(exc).__name__}: {exc}"[:2000]
                raise
            else:
                choice = response.choices[0]
                call["raw"] = choice.message.content
                call["refusal"] = choice.message.refusal
                call["finish_reason"] = choice.finish_reason
                call["usage"] = labproto.usage_dict(response.usage)
                return response
            finally:
                call["latency_ms"] = int((time.perf_counter() - t0) * 1000)
                if call["usage"]:
                    call["cost_usd"] = llm._cost_usd(
                        call["model"],
                        call["usage"]["prompt_tokens"],
                        call["usage"]["completion_tokens"],
                    )

    class _Beta:
        class chat:  # noqa: N801 — mirrors client.beta.chat.completions
            completions = _ParseCompletions()

    class _ClientProxy:
        beta = _Beta()

        # Only beta.chat.completions.parse is recorded. Anything else, such as
        # the Anthropic path's messages.parse, goes to the real client unrecorded.
        def __getattr__(self, name):
            return getattr(real_client_factory(), name)

    llm._client = lambda: _ClientProxy()

    # --- structured_completion: what each attempt raised -----------------------
    # These wrappers replace module attributes. They take effect because the
    # callers look the name up at call time: triage.py calls
    # llm.structured_completion(...), main.py calls triage.classify_ticket(...)
    # and errors.user_message(...).
    real_structured = llm.structured_completion

    def structured_completion(messages, response_model, *args, **kwargs):
        """One repair-loop attempt: the messages it sent and the DeskhandError it raised, if any."""
        attempt = {
            "n": len(attempts) + 1,
            "messages": copy.deepcopy(messages),
            "call": len(calls) + 1,
            "error": None,
        }
        attempts.append(attempt)
        try:
            return real_structured(messages, response_model, *args, **kwargs)
        except errors.DeskhandError as exc:
            attempt["error"] = {"code": exc.internal_error_code, "detail": exc.detail}
            raise
        except BaseException as exc:
            attempt["error"] = {
                "code": None,
                "uncaught": f"{type(exc).__name__}: {exc}"[:2000],
            }
            raise
        finally:
            ch.emit("attempt.finished", n=attempt["n"], error=attempt["error"])

    llm.structured_completion = structured_completion

    # --- classify_ticket + user_message ---------------------------------------
    real_classify = triage.classify_ticket

    def classify_ticket(ticket_arg, request_id):
        """The request_id main.py generated (emitted at once), and the result or error."""
        observed["request_id"] = request_id
        ch.emit("request_id", request_id=request_id)
        try:
            result, cost_usd = real_classify(ticket_arg, request_id)
        except errors.DeskhandError as exc:
            observed["error"] = {"code": exc.internal_error_code, "detail": exc.detail}
            raise
        observed["result"] = result.model_dump()
        observed["reported_cost_usd"] = cost_usd
        return result, cost_usd

    triage.classify_ticket = classify_ticket

    real_user_message = errors.user_message

    def user_message(code):
        """The customer-safe text main.py picked for an error code."""
        text = real_user_message(code)
        observed["user_message"] = text
        return text

    errors.user_message = user_message

    # --- one-ticket dataset for main.main() -----------------------------------
    # main.main() reads TICKETS_PATH when it runs, so pointing it at a temp
    # file with one ticket runs the real CLI on that one ticket.
    tmp_dir = tempfile.mkdtemp(prefix="deskhand-lab-")
    tickets_path = Path(tmp_dir) / "tickets.json"
    tickets_path.write_text(json.dumps({"tickets": [ticket]}), encoding="utf-8")
    original_tickets_path = triage_main.TICKETS_PATH
    triage_main.TICKETS_PATH = tickets_path

    log_path = Path(obslog.LOG_PATH)
    log_offset = (
        log_path.stat().st_size if log_path.exists() else 0
    )  # read back only what this run appends

    crash = None
    try:
        triage_main.main()
    except BaseException:
        crash = traceback.format_exc()
    finally:
        triage_main.TICKETS_PATH = original_tickets_path
        shutil.rmtree(tmp_dir, ignore_errors=True)

    log_lines: list[dict] = []
    if log_path.exists() and observed["request_id"]:
        with log_path.open("rb") as fh:
            fh.seek(log_offset)
            for raw_line in fh.read().decode("utf-8", errors="replace").splitlines():
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if record.get("request_id") == observed["request_id"]:
                    log_lines.append(record)

    # Pair each attempt with the provider call it made.
    for attempt in attempts:
        attempt["provider_call"] = next(
            (c for c in calls if c["n"] == attempt["call"]), None
        )
    # What the repair loop appended to the conversation since the previous attempt.
    for i, attempt in enumerate(attempts):
        previous = attempts[i - 1]["messages"] if i else []
        attempt["added_messages"] = attempt["messages"][len(previous) :] if i else []

    # billed: summed over every provider call the proxy saw, failed ones included.
    # reported: what classify_ticket() returned. The lab shows both side by side.
    billed = sum(c.get("cost_usd") or 0.0 for c in calls)
    ch.emit(
        "trace",
        checkpoint="01",
        model=llm.DEFAULT_MODEL,
        provider=llm.PROVIDER,
        ticket=ticket,
        request_id=observed["request_id"],
        result=observed["result"],
        error=observed["error"],
        user_message=observed["user_message"],
        attempts=attempts,
        max_repair_attempts=triage.MAX_REPAIR_ATTEMPTS,
        reported_cost_usd=observed["reported_cost_usd"],
        billed_cost_usd=billed,
        injected={"truncate_attempts": truncate_attempts}
        if truncate_attempts
        else None,
        schema=models.TriageResult.model_json_schema(),
        log_path=str(log_path),
        log_lines=log_lines,
        crash=crash,
        stdout=cap.text(),
        outcome="crashed"
        if crash
        else ("failed" if observed["error"] else "validated"),
        duration_ms=int((time.perf_counter() - started) * 1000),
        startup=labproto.startup(),
    )


def main() -> None:
    labproto.preload(preload_list())
    req = labproto.read_request()
    ch = labproto.Channel()
    labproto.use_checkpoint_dir()
    os.environ.setdefault("COLUMNS", "110")  # rich table width when not a TTY
    cap = labproto.CapturedStdout(
        ch, live=False
    )  # main.py's rich table goes in the trace, not streamed
    sys.stdout = cap
    try:
        if req.get("mode") == "quote":
            quote(req, ch)
        else:
            run(req, ch, cap)
    except BaseException:
        ch.emit("error", message="runner failed", detail=traceback.format_exc())


if __name__ == "__main__":
    main()
