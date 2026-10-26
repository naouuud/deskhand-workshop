"""SQLite persistence for run state and the SSE event log.

The graph runner saves the full state after EVERY node, together with the name
of the next node to run. That single decision buys us:

- debuggability: `GET /runs/{id}` always shows the exact persisted state;
- recovery: if the process dies mid-run, `find_resumable()` returns the stuck
  runs and the graph re-enters at the recorded next node;
- SSE replay: events are appended with a per-run sequence number, so a client
  reconnecting with Last-Event-ID gets exactly the events it missed.

Connections are opened per call (cheap for SQLite, and thread-safe by
construction — the graph runs in worker threads).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from models import TERMINAL_STATUSES, TicketResolutionState

# One SQLite file per checkpoint folder. Tests pass their own db_path (a
# temp file), so they never touch this one.
DB_PATH = Path(__file__).resolve().parent / "runs.db"

# runs: one row per run, overwritten after every node (the latest state, no
# history). events: append-only, one row per event; the (request_id, seq) key
# makes "everything after seq N" an ordered range read.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    request_id TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    next_node  TEXT,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    request_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    event      TEXT NOT NULL,
    data_json  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    PRIMARY KEY (request_id, seq)
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    # timeout=10.0: wait up to 10 s for another thread's write lock instead of
    # failing at once with "database is locked".
    conn = sqlite3.connect(db_path, timeout=10.0)
    # WAL (write-ahead log): readers and the writer do not block each other,
    # so the SSE endpoint can poll while a run thread saves state.
    conn.execute("PRAGMA journal_mode=WAL")
    # Callers use `with _connect(...) as conn`, which commits (or rolls back
    # on an error) but does not close; CPython closes the connection when the
    # last reference to it goes.
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def save_state(
    state: TicketResolutionState,
    next_node: str | None,
    db_path: Path = DB_PATH,
) -> None:
    """Upsert the full state; called after every node."""
    # The whole state as JSON every time, never a partial update. load_state
    # parses it back through the Pydantic model, so a row that no longer fits
    # the schema fails loudly on read.
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (request_id, status, next_node, state_json, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(request_id) DO UPDATE SET status=excluded.status,"
            "   next_node=excluded.next_node, state_json=excluded.state_json,"
            "   updated_at=excluded.updated_at",
            (
                state.request_id,
                state.status,
                next_node,
                state.model_dump_json(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def load_state(
    request_id: str, db_path: Path = DB_PATH
) -> tuple[TicketResolutionState, str | None] | None:
    """Return (state, next_node) for a run, or None if unknown."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT state_json, next_node FROM runs WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    if row is None:
        return None
    return TicketResolutionState.model_validate_json(row[0]), row[1]


def find_resumable(
    db_path: Path = DB_PATH,
) -> list[tuple[TicketResolutionState, str | None]]:
    """Runs stuck in a non-terminal state (except those waiting on a human)."""
    # Three placeholders: the two terminal statuses and awaiting_approval. A
    # paused run is not stuck, and restarting it would skip the human. sorted()
    # gives the frozenset a fixed order. If TERMINAL_STATUSES grows, the
    # placeholder count must grow with it.
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT state_json, next_node FROM runs WHERE status NOT IN (?, ?, ?)",
            (*sorted(TERMINAL_STATUSES), "awaiting_approval"),
        ).fetchall()
    return [
        (TicketResolutionState.model_validate_json(state_json), next_node)
        for state_json, next_node in rows
    ]


def append_event(
    request_id: str, event: str, data: dict, db_path: Path = DB_PATH
) -> int:
    """Append one event to the run's log; returns its per-run sequence number."""
    # seq counts per run, not globally: a stream is one run, and Last-Event-ID
    # is a position in that stream. One thread drives a run at a time, so
    # MAX + 1 does not race in practice; if it did, the primary key would
    # reject the second insert instead of storing two events with one id.
    with _connect(db_path) as conn:
        (seq,) = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO events (request_id, seq, event, data_json, ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                request_id,
                seq,
                event,
                # default=str: a value JSON cannot encode is stored as text
                # instead of failing the node that emitted it.
                json.dumps(data, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return seq


def events_since(
    request_id: str, after_seq: int, db_path: Path = DB_PATH
) -> list[tuple[int, str, dict]]:
    """Events with seq > after_seq, oldest first: [(seq, event, data), ...]."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT seq, event, data_json FROM events"
            " WHERE request_id = ? AND seq > ? ORDER BY seq",
            (request_id, after_seq),
        ).fetchall()
    return [(seq, event, json.loads(data_json)) for seq, event, data_json in rows]
