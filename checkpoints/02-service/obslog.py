"""Tiny JSONL observability log.

Every event is one JSON line in logs/runs.jsonl with at least
{ts, request_id, event, ...}. No logging frameworks — a file and json.dumps.

Why it matters: v1 had no request ids and no logs, so a failed run was
undebuggable. Here every node, repair, tool call, error code and cost lands
in this file under the run's request_id, in order. Events outside any run
(service startup) use the request_id "startup".
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Module-level so tests/conftest.py can point it at a temp file. logs/ is
# gitignored.
LOG_PATH = Path(__file__).resolve().parent / "logs" / "runs.jsonl"
# The service runs each ticket on its own thread. One writer at a time keeps
# two events from interleaving inside one line.
_lock = threading.Lock()


def log_event(request_id: str, event: str, **fields: Any) -> None:
    """Append one structured event line to the run log."""
    # request_id comes first from 02 on (01 had event first), the same order
    # as events.publish(request_id, event, ...).
    # One JSON object per line (JSONL): every line stands alone, so grep, jq
    # or a ten-line script can read the file without a parser for the whole.
    record: dict[str, Any] = {
        # UTC ISO-8601: sorts as text, and no guessing which timezone a server
        # logged in.
        "ts": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "event": event,
        # Caller fields (ticket_id, cost_usd, internal_error_code, ...) sit at
        # the top level, so one grep or jq filter finds them.
        **fields,
    }
    # default=str: a value json cannot encode (a datetime, a Path) is logged
    # as its str() instead of raising inside the node that logged it.
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
