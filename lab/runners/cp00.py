"""Lab runner for checkpoint 00 — observes the broken bot from the outside.

Started by lab/server.py with cwd = checkpoints/00-broken-bot (bot.py globs
../../shared/policies relative to the cwd). bot.py is imported unmodified and
bot.main() runs exactly as `py bot.py "<ticket>"` would. Everything the lab
reports is recorded around it:

- bot.client is swapped for a recording proxy that forwards every call to the
  real client. The proxy adds stream_options.include_usage so the provider
  reports token counts, and swallows that one extra usage-only chunk, so the
  chunks bot.py iterates are exactly the ones it would get without the lab.
- sys.settrace watches bot.resolve and bot.main: the exceptions the bare
  `except Exception:` swallows, the value resolve() returns, and whether the
  "refund issued" line executed.

Request (stdin): {"mode": "run", "ticket_text": "..."} or {"mode": "list"}.
"""

from __future__ import annotations

import hashlib
import json
import linecache
import re
import sys
import time
import traceback
from pathlib import Path

import labproto

# Third-party packages bot.py imports, loaded while the runner waits warm (labproto.preload).
PRELOAD = ("openai", "dotenv")

_MISSING = object()  # "resolve() never returned", as distinct from returning None


def _sha(obj) -> str:
    """Fingerprint of the messages sent: equal hashes prove a retry resent the identical prompt."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


def list_incidents(ch: labproto.Channel) -> None:
    """{"mode": "list"}: the Session 1 demos, read from incidents.py for the ticket picker."""
    import incidents  # noqa: imports bot too — no network call happens at import

    ch.emit(
        "incidents",
        incidents=[
            {"title": title, "watch": watch, "text": text}
            for title, watch, text in incidents.INCIDENTS
        ],
        file=incidents.__file__,
    )


def run(req: dict, ch: labproto.Channel, cap: labproto.CapturedStdout) -> None:
    """{"mode": "run"}: bot.main() once on ticket_text, then one "trace".

    On the way: attempt.started, attempt.exception and attempt.finished for
    each provider call, and the bot's own print() output as "stdout" events
    (what the customer would have seen, as it streamed)."""
    ticket_text: str = req["ticket_text"]
    started = time.perf_counter()

    try:
        # bot.py builds its client from .env at import time, so a bad .env can fail here.
        import bot
    except BaseException:
        ch.emit(
            "trace",
            checkpoint="00",
            outcome="crashed",
            crash=traceback.format_exc(),
            attempts=[],
            stdout=cap.text(),
            duration_ms=0,
            startup=labproto.startup(),
        )
        return

    attempts: list[dict] = []  # one record per provider call, in order
    first_prompt: dict = {}
    real_client = bot.client

    def public(rec: dict) -> dict:
        """The record without lab-internal keys (_t0)."""
        return {k: v for k, v in rec.items() if not k.startswith("_")}

    # --- the recording proxy: bot.client.chat.completions.create -------------
    # bot.py calls bot.client.chat.completions.create(...) through the module
    # global, so swapping bot.client is enough. Every call still reaches the
    # real client with the arguments bot.py chose.

    class _StreamTap:
        """Wraps the provider's stream. bot.py iterates it as usual; the tap
        copies the text, finish_reason and usage into the attempt record and
        emits the record when the stream ends, however it ends."""

        def __init__(self, stream, rec: dict) -> None:
            self._stream = stream
            self._rec = rec

        def __iter__(self):
            rec = self._rec
            try:
                for chunk in self._stream:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        rec["usage"] = labproto.usage_dict(usage)
                        if not chunk.choices:
                            continue  # the lab's extra usage chunk — bot.py never sees it
                    if chunk.choices:
                        choice = chunk.choices[0]
                        if choice.delta is not None and choice.delta.content:
                            rec["raw"] += choice.delta.content
                        if choice.finish_reason:
                            rec["finish_reason"] = choice.finish_reason
                    yield chunk
            except GeneratorExit:  # bot.py stopped iterating early: not a stream error
                raise
            except BaseException as exc:
                rec["stream_error"] = f"{type(exc).__name__}: {exc}"[:2000]
                raise
            finally:
                rec["latency_ms"] = int((time.perf_counter() - rec["_t0"]) * 1000)
                ch.emit("attempt.finished", attempt=public(rec))

    class _Completions:
        def create(self, **kwargs):
            """Record the request, call the real client, hand back a tapped stream."""
            messages = kwargs.get("messages") or []
            prompt_text = "\n".join(str(m.get("content", "")) for m in messages)
            rec = {
                "n": len(attempts) + 1,
                "sha256": _sha(messages),
                "prompt_chars": len(prompt_text),
                "model": kwargs.get("model"),
                "temperature": kwargs.get("temperature"),
                "stream": bool(kwargs.get("stream")),
                "raw": "",
                "usage": None,
                "finish_reason": None,
                "exception": None,
                "api_error": None,
                "stream_error": None,
                "lab_added": None,
                "_t0": time.perf_counter(),
            }
            if not attempts:
                first_prompt.update(
                    text=prompt_text, sha256=rec["sha256"], messages=len(messages)
                )
                rec["identical_to_first"] = True
            else:
                rec["identical_to_first"] = rec["sha256"] == attempts[0]["sha256"]
                if not rec[
                    "identical_to_first"
                ]:  # keep a retry's prompt only when it changed
                    rec["prompt_text"] = prompt_text
            attempts.append(rec)
            ch.emit(
                "attempt.started",
                n=rec["n"],
                sha256=rec["sha256"],
                identical_to_first=rec["identical_to_first"],
            )

            # The one change to the request, recorded in lab_added so the trace says so.
            sent = dict(kwargs)
            if sent.get("stream") and "stream_options" not in sent:
                sent["stream_options"] = {"include_usage": True}
                rec["lab_added"] = "stream_options={'include_usage': True}"
            try:
                result = real_client.chat.completions.create(**sent)
            except BaseException as exc:
                rec["api_error"] = f"{type(exc).__name__}: {exc}"[:2000]
                rec["latency_ms"] = int((time.perf_counter() - rec["_t0"]) * 1000)
                ch.emit("attempt.finished", attempt=public(rec))
                raise
            if not sent.get("stream"):
                return result
            return _StreamTap(result, rec)

    class _Chat:
        completions = _Completions()

    class _ClientProxy:
        chat = _Chat()

        def __getattr__(self, name):  # anything else goes straight to the real client
            return getattr(real_client, name)

    # --- what to watch, located in bot.py by content, not hardcoded numbers ---
    # sys.settrace instead of a wrapper: resolve() swallows its own exceptions
    # with a bare except, so nothing outside it ever sees them. A trace
    # function sees each one as it is raised, without editing bot.py.
    resolve_code = bot.resolve.__code__
    main_code = bot.main.__code__
    bot_file = str(Path(bot.__file__).resolve())
    source = Path(bot_file).read_text(encoding="utf-8").splitlines()
    # Only lines below `def main`, so a mention earlier in the file doesn't count.
    refund_issued_lines = {
        i + 1
        for i, line in enumerate(source)
        if "refund issued" in line and i + 1 > main_code.co_firstlineno
    }
    observed: dict = {"returned": _MISSING, "refund_lines_hit": [], "resolve_calls": 0}

    def trace_resolve(frame, event, arg):
        """Local trace for resolve(): every exception raised, and the return value."""
        if event == "exception":
            etype, evalue, tb = arg
            if etype in (StopIteration, GeneratorExit):  # a loop ending, not a failure
                return trace_resolve
            # Innermost frame = where it was raised (often json/decoder.py).
            inner = tb
            while inner is not None and inner.tb_next is not None:
                inner = inner.tb_next
            info = {
                "type": etype.__name__,
                "message": str(evalue)[:2000],
                "file": frame.f_code.co_filename,
                "line": frame.f_lineno,
                "code": linecache.getline(
                    frame.f_code.co_filename, frame.f_lineno
                ).strip(),
                "raised_in": (
                    f"{Path(inner.tb_frame.f_code.co_filename).name}:{inner.tb_lineno}"
                    if inner is not None
                    else None
                ),
            }
            if attempts:
                attempts[-1]["exception"] = info
            ch.emit(
                "attempt.exception",
                n=attempts[-1]["n"] if attempts else None,
                exception=info,
            )
        elif event == "return":
            observed["returned"] = arg
        return trace_resolve

    def trace_main(frame, event, arg):
        """Line events in main(): did the "refund issued" line run?"""
        if event == "line" and frame.f_lineno in refund_issued_lines:
            observed["refund_lines_hit"].append(frame.f_lineno)
        return trace_main

    def global_trace(frame, event, arg):
        """Called on every function call: trace resolve() and main() frames
        only. Returning None leaves every other frame untraced, at full speed."""
        if event != "call":
            return None
        if frame.f_code is resolve_code:
            observed["resolve_calls"] += 1
            frame.f_trace_lines = False  # exceptions and the return value are enough
            return trace_resolve
        if frame.f_code is main_code:
            return trace_main
        return None

    # --- run bot.main() exactly as `py bot.py "<ticket_text>"` would -----------
    crash = None
    sys.argv = [
        "bot.py",
        ticket_text,
    ]  # bot.main() reads the ticket from argv, as on the command line
    bot.client = _ClientProxy()
    sys.settrace(global_trace)
    try:
        bot.main()
    except BaseException:
        crash = traceback.format_exc()
    finally:
        sys.settrace(None)  # nothing below is traced or proxied
        bot.client = real_client

    # The outcome is what resolve() handed back: None means it gave up after
    # its retries; parsed JSON that is not an object is not a plan.
    returned = observed["returned"]
    if crash:
        outcome = "crashed"
    elif returned is _MISSING:
        outcome = "not_called"
    elif returned is None:
        outcome = "gave_up"
    elif isinstance(returned, dict):
        outcome = "parsed"
    else:
        outcome = "parsed_non_object"

    plan = None if returned is _MISSING else returned
    refund_amount = plan.get("refund_amount") if isinstance(plan, dict) else None
    policies = getattr(bot, "POLICIES", "")

    ch.emit(
        "trace",
        checkpoint="00",
        model=getattr(bot, "MODEL", None),
        ticket_text=ticket_text,
        prompt={
            **first_prompt,
            "chars": len(first_prompt.get("text", "")),
            "policies_chars": len(policies),
            "policy_titles": re.findall(r"^# (.+)$", policies, re.M),
        },
        attempts=[public(a) for a in attempts],
        outcome=outcome,
        plan=plan,
        refund={
            "amount": refund_amount,
            "fired": bool(observed["refund_lines_hit"])
            if refund_issued_lines
            else None,
            "line": min(refund_issued_lines) if refund_issued_lines else None,
        },
        crash=crash,
        stdout=cap.text(),
        bot_file=bot_file,
        resolve_line=resolve_code.co_firstlineno,
        duration_ms=int((time.perf_counter() - started) * 1000),
        startup=labproto.startup(),
    )


def main() -> None:
    labproto.preload(PRELOAD)
    req = labproto.read_request()
    ch = labproto.Channel()
    labproto.use_checkpoint_dir()
    # Live on a run: bot.py streams its reply with print(), and that text is the customer's view.
    cap = labproto.CapturedStdout(ch, live=req.get("mode") == "run")
    sys.stdout = cap
    try:
        if req.get("mode") == "list":
            list_incidents(ch)
        else:
            run(req, ch, cap)
    except BaseException:
        # Still end with a terminal event: server.py gets the traceback, not only an exit code.
        ch.emit("error", message="runner failed", detail=traceback.format_exc())


if __name__ == "__main__":
    main()
