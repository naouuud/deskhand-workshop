"""Append-only JSONL observability log, one JSON object per line.

Observability means being able to answer "what happened on that run?" after
the fact. With an LLM in the loop you need this more than usual: the same
input can produce a different answer tomorrow, and every call costs money, so
you want the tokens, latency and cost of each call written down.

Every event carries at least {ts, request_id, event}. No logging frameworks;
the point is that a failed run is greppable by request_id.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Module-level so tests/conftest.py can point it at a temp file. logs/ is
# gitignored.
LOG_PATH = Path(__file__).resolve().parent / "logs" / "runs.jsonl"


def log_event(event: str, request_id: str, **fields: Any) -> None:
    # Example call: log_event("triage.completed", rid, ticket_id="T-1001",
    # cost_usd=0.0002). **fields collects those extra keyword arguments into a
    # dict, so each caller can attach whatever is useful for that event.
    #
    # One JSON object per line (JSONL): every line stands alone, so grep, jq
    # or a ten-line script can read the file without a parser for the whole.
    record = {
        # UTC ISO-8601: sorts as text, and no guessing which timezone a server
        # logged in.
        "ts": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "event": event,
        # Caller fields (ticket_id, cost_usd, internal_error_code, ...) sit at
        # the top level, so one grep or jq filter finds them.
        **fields,
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Open, append, close on every event: nothing waits in a buffer when the
    # process dies. ensure_ascii=False keeps customer names readable.
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
