"""Plain-Python graph runner. No frameworks — a dict of nodes and a while loop.

Contract with nodes:
- a node returns the NAME of the next node, nodes.PAUSE (human takes over), or
  None (terminal);
- state is persisted after EVERY node together with the next node's name, so a
  crashed process can resume exactly where it stopped;
- a DeskhandError raised by a node routes to handle_failure with its taxonomy
  code; any other exception becomes INTERNAL_UNEXPECTED.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import errors
import events
import nodes
import obslog
import store
from models import TicketResolutionState

ENTRY = "normalize_ticket"


def run(
    state: TicketResolutionState,
    start: str = ENTRY,
    db_path: Path = store.DB_PATH,
) -> TicketResolutionState:
    """Drive the state through the graph from `start` until pause or terminal."""

    # Every event a node sends goes through emit. events.publish writes it to
    # SQLite before any client sees it, so an SSE reconnect can replay it.
    def emit(event: str, data: dict) -> int:
        return events.publish(state.request_id, event, data, db_path=db_path)

    # The whole runtime is this loop. Each node is a small function with one
    # job, and its code returns the next node's name: the model fills in the
    # state, it never picks the edge. PAUSE ends this thread without ending
    # the run; POST /runs/{id}/approve starts a new thread later.
    node: str | None = start
    while node is not None and node != nodes.PAUSE:
        emit(events.NODE_STARTED, {"node": node})
        started = time.perf_counter()
        cost_before = state.cost_usd  # node.completed reports what this node alone spent
        # Error routing. A DeskhandError carries the taxonomy code (errors.py)
        # picked where the failure happened; anything else is a bug and gets
        # INTERNAL_UNEXPECTED. Either way the next node is handle_failure,
        # which shows the customer a safe message, never the exception.
        # KeyboardInterrupt is a BaseException, not an Exception: it passes
        # through like a real crash, which tests/test_recovery.py relies on.
        try:
            next_node = nodes.NODES[node](state, emit)
        except Exception as exc:
            state.internal_error_code = (
                exc.internal_error_code
                if isinstance(exc, errors.DeskhandError)
                else errors.INTERNAL_UNEXPECTED
            )
            obslog.log_event(
                state.request_id,
                "node.error",
                node=node,
                internal_error_code=state.internal_error_code,
                detail=str(exc),
            )
            if node == "handle_failure":  # never loop on the failure handler
                state.status = "failed"
                next_node = None
            else:
                next_node = "handle_failure"

        latency_ms = int((time.perf_counter() - started) * 1000)
        # Keyed by name: a node visited twice (validate_plan after a repair)
        # keeps its latest latency.
        state.node_latency_ms[node] = latency_ms
        # A paused run is stored with next_node=None: nothing restarts it on
        # its own. Only a human decision does.
        stored_next = next_node if next_node != nodes.PAUSE else None
        # The checkpoint. The full state and the next node's name are saved
        # after every node, before the next one starts, so a crash loses only
        # the node that was running. resume() runs that node again, which is
        # why execute_actions sends an idempotency key with every refund.
        store.save_state(state, next_node=stored_next, db_path=db_path)
        # Sent after the save: a client never sees a node as done before its
        # state is on disk.
        emit(
            events.NODE_COMPLETED,
            {
                "node": node,
                "latency_ms": latency_ms,
                "cost_usd": round(state.cost_usd, 6),
                "cost_delta_usd": round(state.cost_usd - cost_before, 6),
            },
        )
        node = next_node

    return state


def run_in_thread(
    state: TicketResolutionState,
    start: str = ENTRY,
    db_path: Path = store.DB_PATH,
) -> threading.Thread:
    """Start a run on a background thread and return at once.

    POST /tickets answers with the request_id while the run goes on; the
    client follows it over SSE. daemon=True: stopping the server does not
    wait for runs. resume_stuck_runs picks up a run cut off mid-node on the
    next start.
    """
    thread = threading.Thread(
        target=run, args=(state, start, db_path), daemon=True, name=state.request_id
    )
    thread.start()
    return thread


def resume(
    state: TicketResolutionState,
    next_node: str | None,
    db_path: Path = store.DB_PATH,
) -> TicketResolutionState:
    """Continue a run that stopped after a node: re-enter at the recorded
    next node. (graph_lg.py has the same signature but asks LangGraph's
    checkpointer where to continue instead of trusting `next_node`.)"""
    # A non-terminal run with no recorded next node should not exist. If one
    # does, end it through handle_failure instead of guessing where it was.
    return run(state, start=next_node or "handle_failure", db_path=db_path)


def resume_after_approval(
    state: TicketResolutionState,
    approved: bool,
    db_path: Path = store.DB_PATH,
) -> threading.Thread:
    """Approved: execute the write actions. Rejected: skip execution and tell
    the customer a human took over (draft_customer_reply handles that case)."""
    # The plain runner kept no record of where it paused, so the caller names
    # the node. app.py has already set state.approval_status, which
    # draft_customer_reply reads to write the "a human took over" reply.
    start = "execute_actions" if approved else "draft_customer_reply"
    return run_in_thread(state, start=start, db_path=db_path)


def resume_stuck_runs(db_path: Path = store.DB_PATH) -> list[str]:
    """On startup: re-launch runs that died in a non-terminal state.

    Safe because state was saved after the last completed node and refunds are
    idempotent — re-entering execute_actions cannot pay twice.
    """
    resumed: list[str] = []
    # find_resumable leaves out runs awaiting approval: those wait for a
    # person, not for a restart.
    for state, next_node in store.find_resumable(db_path=db_path):
        obslog.log_event(state.request_id, "run.resumed", next_node=next_node)
        threading.Thread(
            target=resume,
            args=(state, next_node, db_path),
            daemon=True,
            name=state.request_id,
        ).start()
        resumed.append(state.request_id)
    return resumed
