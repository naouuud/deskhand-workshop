"""Wire protocol between a lab runner subprocess and lab/server.py.

A runner is started with cwd = one checkpoint folder. It reads ONE JSON request
from stdin and writes NDJSON events to the ORIGINAL stdout. The checkpoint's
own print() output is captured and forwarded as {"type": "stdout"} events, so
nothing the checkpoint prints can corrupt the protocol.

This module is named `labproto` so it can never collide with a checkpoint
module (bot, llm, errors, models, triage, obslog, ...).
"""

# Every runner's main() does the same steps, in this order:
#   0. preload(PRELOAD)       import the provider SDKs before the request exists
#   1. read_request()         the JSON request server.py wrote to stdin
#   2. Channel()              take the real stdout pipe; fd 1 now goes to stderr
#   3. use_checkpoint_dir()   the checkpoint folder first on sys.path
#   4. sys.stdout = CapturedStdout(...)   the checkpoint's print() becomes events
# Then it runs one mode and ends with one terminal event: the mode's
# result, or {"type": "error"} from its except. server.py treats a runner
# that exits without one as crashed.
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import time
from typing import Any

# Filled by preload() and read_request(); runners put startup() in their trace.
_ready_at: float | None = None  # wall clock when preload() finished
_startup_ms: int | None = None  # how long the run waited on this process to start


def preload(modules: tuple[str, ...]) -> None:
    """Import the third-party packages the checkpoint is about to import.

    server.py starts the next runner before anyone asks for a run (its warm
    pool), so this happens while the runner idles in read_request(). The SDKs
    are thousands of files, which takes seconds where an antivirus scans each
    one. Only third-party packages belong here: the checkpoint's own modules
    and its .env load after the request arrives, so an edited checkpoint is
    picked up and a model change made in the lab is seen. sys.path[0] is
    lab/runners at this point, so nothing here can import a checkpoint module.
    """
    global _ready_at
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - the checkpoint's own import raises it again, into the trace
            pass
    _ready_at = time.time()


class Channel:
    """The runner's only way to talk to server.py: one JSON object per line."""

    def __init__(self) -> None:
        # Keep a private handle on the real stdout pipe, then point fd 1 at
        # stderr so any stray C-level write cannot break the NDJSON stream.
        # server.py drains stderr and shows its tail only if the runner dies.
        self._out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
        os.dup2(2, 1)

    def emit(self, type_: str, **data: Any) -> None:
        # default=str: a datetime or Path in a trace becomes text instead of an error.
        self._out.write(
            json.dumps({"type": type_, **data}, default=str, ensure_ascii=False) + "\n"
        )
        self._out.flush()  # one event, one flush: the page sees it now, not at exit


class CapturedStdout(io.TextIOBase):
    """Replaces sys.stdout inside the runner: everything the checkpoint
    prints is forwarded live and kept for the final trace."""

    # live=False keeps the text for the trace only, unstreamed (01's rich table).

    def __init__(self, channel: Channel, live: bool = True) -> None:
        self.channel = channel
        self.live = live
        self.parts: list[str] = []

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:  # rich and friends ask for this
        return "utf-8"

    def write(self, s: str) -> int:
        if s:
            self.parts.append(s)
            if self.live:
                self.channel.emit("stdout", text=s)
        return len(s)

    def flush(self) -> None:
        pass

    def text(self) -> str:
        return "".join(self.parts)


def read_request() -> dict:
    """The one JSON request on stdin. Reads to EOF, which comes when server.py
    closes stdin after writing it."""
    global _startup_ms
    raw = sys.stdin.buffer.read().decode("utf-8")
    if not raw.strip():
        # EOF with no request: server.py stopped (or died) while this runner
        # sat warm in its pool. Leave without running anything.
        sys.exit(0)
    req = json.loads(raw)
    # lab_sent_at: when server.py handed the request over. A warm runner was
    # ready before that (0 ms); a cold one makes the run wait for Python to
    # start and preload() to finish. Same machine, so the clocks agree.
    sent_at = req.pop("lab_sent_at", None)
    if sent_at is not None and _ready_at is not None:
        _startup_ms = max(0, int((_ready_at - sent_at) * 1000))
    return req


def startup() -> dict:
    """For the trace: how long the run waited on this process before its first
    line of checkpoint code. Kept apart from duration_ms, which times the run."""
    return {
        "wait_ms": _startup_ms,
        "warm": _startup_ms is not None and _startup_ms < 100,
    }


def use_checkpoint_dir() -> str:
    """Make the checkpoint folder (the cwd) win imports, exactly as if the
    checkpoint's own script had been run from it."""
    # sys.path[0] is lab/runners (the runner script's folder), which is how
    # `import labproto` works. The checkpoint goes in front of it, so `import
    # llm` finds the checkpoint's llm.py. Nothing in the checkpoint changes.
    cwd = os.getcwd()
    sys.path.insert(0, cwd)
    return cwd


def usage_dict(usage: Any) -> dict | None:
    """An SDK usage object -> the two token counts the lab prices, as a plain dict."""
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
    }
