"""The streaming contract: which events exist and how they are published.

Only the customer reply streams token-wise (reply.delta). Machine output — the
plan, the state — is NEVER streamed partially: a half-built JSON object on an
agent's screen is exactly the v1 incident we are retiring. Terminal states are
explicit events (run.completed / run.failed), never inferred from a closed
connection.

Events are persisted (store.append_event) before delivery, so an SSE client
that reconnects with Last-Event-ID replays what it missed.
"""
from __future__ import annotations

from pathlib import Path

import obslog
import store

# Event types (the frontend contract).
RUN_STATUS = "run.status"            # {status}
NODE_STARTED = "node.started"        # {node}
NODE_COMPLETED = "node.completed"    # {node, latency_ms, cost_usd}
APPROVAL_REQUIRED = "approval.required"  # {plan summary}
REPLY_DELTA = "reply.delta"          # {text} — the ONLY token-wise stream
# A run ends with one of these two. A client waits for one of them and never
# treats a closed connection as a finished run.
RUN_COMPLETED = "run.completed"      # {reply, plan}
RUN_FAILED = "run.failed"            # {user_visible_error}


def publish(
    request_id: str, event: str, data: dict, db_path: Path = store.DB_PATH
) -> int:
    """Persist an event for SSE delivery and mirror it to the JSONL log."""
    # Store first, deliver second. The SSE endpoint only ever reads this table
    # (app.run_events polls store.events_since), so every event is there for
    # every client, including one that reconnects later. The seq returned
    # here is the event's SSE id, which a browser sends back as Last-Event-ID.
    seq = store.append_event(request_id, event, data, db_path=db_path)
    if event != REPLY_DELTA:  # deltas would drown the log; the full reply is logged
        obslog.log_event(request_id, event, **data)
    return seq
