"""The same workflow on LangGraph. Select it with DESKHAND_RUNTIME=langgraph.

graph.py is a dict of nodes and a while loop that we wrote by hand. This
module hands the loop to LangGraph and keeps everything else:

    plain runner (graph.py)                 LangGraph (this file)
    node returns the next node's name       wrapper returns Command(update=..., goto=...)
    approval_gate returns PAUSE             await_approval calls interrupt(...)
    store.save_state after every node       SqliteSaver checkpoint after every step
    resume_stuck_runs restarts at next_node invoke(None) on thread_id = request_id
    approve endpoint restarts at a node     invoke(Command(resume=approved))

What does NOT move into the framework:
- nodes.py. Every node runs unchanged behind one generic wrapper.
- the runs table. GET /runs/{id} and store.find_resumable read it, so the
  wrapper still calls store.save_state after every node. The checkpointer owns
  *where to resume*; the runs table owns *what the API shows*.
- the event log. Reply deltas and node events still go through emit into
  SQLite, so SSE and Last-Event-ID replay are untouched.
- idempotency keys. LangGraph cannot make a refund safe to repeat; the key can.

LangGraph checkpoints live next to runs.db in runs.langgraph.db. Open both and
compare them: same run, two ways of remembering it.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, StateSnapshot, interrupt

import errors
import events
import nodes
import obslog
import store
from models import TicketResolutionState

ENTRY = "normalize_ticket"
AWAIT_APPROVAL = "await_approval"  # LangGraph-only node, deliberately not in nodes.NODES


@dataclass
class RunContext:
    """Per-invocation context, injected into nodes as `runtime.context`.
    Not checkpointed: every invoke passes it again."""

    db_path: Path  # which runs.db, and so which runs.langgraph.db; tests pass a temp one
    start: str = ENTRY  # where _entry routes out of START


# Where each node may go next, read off nodes.py. Command(goto=...) would work
# without this, but declaring it makes compile() reject a typo in a node name
# and lets graph.get_graph().draw_mermaid() draw the real edges.
_FAIL = "handle_failure"
DESTINATIONS: dict[str, tuple[str, ...]] = {
    "normalize_ticket": ("triage", _FAIL),
    "triage": ("fetch_order_data", _FAIL),
    "fetch_order_data": ("generate_resolution_plan", _FAIL),
    "generate_resolution_plan": ("validate_plan", _FAIL),
    # validate_plan has four exits: needs a human, auto-approved, fixable
    # (repair budget left), or REPAIR_EXHAUSTED.
    "validate_plan": ("approval_gate", "execute_actions", "repair_plan", _FAIL),
    "repair_plan": ("validate_plan", _FAIL),
    # The plain runner's PAUSE, made a real edge to the interrupt node.
    "approval_gate": (AWAIT_APPROVAL, _FAIL),
    "execute_actions": ("draft_customer_reply", _FAIL),
    "draft_customer_reply": ("finalize", _FAIL),
    "finalize": (END, _FAIL),
    "handle_failure": (END,),
}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def _entry(state: TicketResolutionState, runtime: Runtime[RunContext]) -> str:
    """Conditional edge out of START: begin wherever the caller asked."""
    return runtime.context.start


def _wrap(name: str):
    """Run nodes.NODES[name] with the plain runner's contract around it:
    node events, error routing, latency, runs-table save. Then tell LangGraph
    what changed (the whole state) and where to go."""
    fn = nodes.NODES[name]

    # This is the plain runner's loop body for one node. What LangGraph takes
    # over is the loop itself: picking the node that runs next.
    def node(state: TicketResolutionState, runtime: Runtime[RunContext]) -> Command:
        db_path = runtime.context.db_path

        def emit(event: str, data: dict) -> int:
            return events.publish(state.request_id, event, data, db_path=db_path)

        emit(events.NODE_STARTED, {"node": name})
        started = time.perf_counter()
        cost_before = state.cost_usd  # node.completed reports what this node alone spent
        # interrupt() pauses a run by raising GraphInterrupt, a GraphBubbleUp.
        # The first except lets it through; without it, `except Exception`
        # would turn a pause into a node failure. No node in nodes.py calls
        # interrupt() today, so this guards the wrapper against a future one.
        try:
            next_node = fn(state, emit)
        except GraphBubbleUp:  # LangGraph control flow, never a node failure
            raise
        except Exception as exc:  # same routing as graph.py
            state.internal_error_code = (
                exc.internal_error_code
                if isinstance(exc, errors.DeskhandError)
                else errors.INTERNAL_UNEXPECTED
            )
            obslog.log_event(
                state.request_id,
                "node.error",
                node=name,
                internal_error_code=state.internal_error_code,
                detail=str(exc),
            )
            if name == "handle_failure":  # never loop on the failure handler
                state.status = "failed"
                next_node = None
            else:
                next_node = "handle_failure"

        latency_ms = int((time.perf_counter() - started) * 1000)
        state.node_latency_ms[name] = latency_ms
        stored_next = next_node if next_node != nodes.PAUSE else None
        # runs.db is written here, inside the node; LangGraph's checkpoint is
        # written after the node returns. A crash between the two leaves the
        # checkpoint one node behind, and resume runs this node again (its
        # events appear twice). The money is safe: refunds carry an
        # idempotency key.
        store.save_state(state, next_node=stored_next, db_path=db_path)
        emit(
            events.NODE_COMPLETED,
            {
                "node": name,
                "latency_ms": latency_ms,
                "cost_usd": round(state.cost_usd, 6),
                "cost_delta_usd": round(state.cost_usd - cost_before, 6),
            },
        )
        # Nodes mutate `state` in place, as under the plain runner. LangGraph
        # keeps only what a node returns, so the wrapper hands back the whole
        # state and translates the routing: PAUSE becomes the interrupt node,
        # None becomes END.
        goto = AWAIT_APPROVAL if next_node == nodes.PAUSE else (next_node or END)
        # A dict, not the model: LangGraph filters a Pydantic update down to
        # non-default fields, which would silently drop a field reset to None.
        return Command(update=state.model_dump(), goto=goto)

    node.__name__ = name
    return node


def _await_approval(
    state: TicketResolutionState, runtime: Runtime[RunContext]
) -> Command:
    """Wait for a human. interrupt() stops the run here; Command(resume=...)
    restarts this node FROM THE TOP and interrupt() returns the decision.

    This is a separate node, not part of approval_gate, because of that
    re-execution: anything before interrupt() runs twice. approval_gate emits
    approval.required, so it must finish before the run pauses."""
    plan = state.resolution_plan
    # First pass: interrupt() saves a checkpoint holding this payload and stops
    # the run; invoke() returns and the thread ends. On Command(resume=...)
    # this node starts again and interrupt() returns the decision. The payload
    # mirrors approval.required: the ask, the order total, the ownership check
    # and the reasons, so the approver sees the gap either way.
    decision = interrupt(
        {
            "request_id": state.request_id,
            "ticket_id": plan.ticket_id if plan else state.ticket_id,
            "actions": [a.type for a in plan.actions] if plan else [],
            "refund_amount_usd": plan.refund_amount_usd if plan else None,
            "approval_reasons": state.approval_reasons,
            "requested_refund_usd": plan.requested_refund_usd if plan else None,
            "order_total_usd": (state.order_data or {}).get("total_usd"),
            "customer_match": state.customer_match,
            "rationale": plan.rationale if plan else None,
            "confidence": plan.confidence if plan else None,
        }
    )
    state.approval_status = "approved" if decision else "rejected"
    target = "execute_actions" if decision else "draft_customer_reply"
    # Keep the runs table in step, so GET /runs/{id} shows the decision and
    # the next node before that node starts.
    store.save_state(state, next_node=target, db_path=runtime.context.db_path)
    return Command(update={"approval_status": state.approval_status}, goto=target)


def build() -> StateGraph:
    """Declare the graph: each node behind the wrapper, the approval node, and
    one conditional edge out of START. There are no add_edge calls: every node
    routes itself with Command(goto=...), and `destinations` only tells
    compile() and get_graph() which edges exist."""
    builder = StateGraph(TicketResolutionState, context_schema=RunContext)
    for name in nodes.NODES:
        builder.add_node(name, _wrap(name), destinations=DESTINATIONS[name])
    builder.add_node(
        AWAIT_APPROVAL,
        _await_approval,
        destinations=("execute_actions", "draft_customer_reply"),
    )
    builder.add_conditional_edges(START, _entry, path_map=list(nodes.NODES))
    return builder


# ---------------------------------------------------------------------------
# One compiled graph + checkpointer per runs database
# ---------------------------------------------------------------------------

# Compiled once per runs database and cached: the checkpointer is bound to one
# file, and every request after the first reuses the same graph and
# connection. The lock stops two first requests from opening two.
_compiled_graphs: dict[Path, tuple[CompiledStateGraph, sqlite3.Connection]] = {}
_lock = threading.Lock()


def checkpoint_path(db_path: Path = store.DB_PATH) -> Path:
    """runs.db -> runs.langgraph.db, in the same folder."""
    return Path(db_path).with_suffix(".langgraph.db")


def _compiled(db_path: Path) -> CompiledStateGraph:
    key = Path(db_path).resolve()
    with _lock:
        if key not in _compiled_graphs:
            # One connection shared by every thread; SqliteSaver serialises
            # access with its own lock and creates its tables on first use.
            conn = sqlite3.connect(str(checkpoint_path(key)), check_same_thread=False)
            compiled = build().compile(checkpointer=SqliteSaver(conn))
            _compiled_graphs[key] = (compiled, conn)
        return _compiled_graphs[key][0]


def close_all() -> None:
    """Close every checkpoint connection. For tests; never during a run."""
    with _lock:
        for _, conn in _compiled_graphs.values():
            conn.close()
        _compiled_graphs.clear()


# thread_id is LangGraph's key for one sequence of checkpoints. Using the
# request_id makes one run one thread, so runs.db and runs.langgraph.db share
# a key.
def _config(request_id: str) -> dict:
    # Built fresh per call: LangGraph writes into config["configurable"].
    return {"configurable": {"thread_id": request_id}}


# durability="sync" below: LangGraph's default ("async") writes a step's
# checkpoint while the next step already runs, so a crash could lose it.
# With "sync", a crash inside node N leaves a checkpoint that says "run N".
def _invoke(payload, request_id: str, db_path: Path, start: str = ENTRY) -> None:
    _compiled(db_path).invoke(
        payload,
        _config(request_id),
        context=RunContext(db_path=Path(db_path), start=start),
        durability="sync",  # checkpoint before the next node starts, not after
    )


def checkpoint_state(request_id: str, db_path: Path = store.DB_PATH) -> StateSnapshot:
    """LangGraph's view of a run: .values, .next, .interrupts."""
    return _compiled(db_path).get_state(_config(request_id))


def checkpoint_history(
    request_id: str, db_path: Path = store.DB_PATH
) -> list[StateSnapshot]:
    """Every checkpoint of a run, newest first: one per step."""
    return list(_compiled(db_path).get_state_history(_config(request_id)))


# ---------------------------------------------------------------------------
# The runtime interface shared with graph.py
# ---------------------------------------------------------------------------

def run(
    state: TicketResolutionState,
    start: str = ENTRY,
    db_path: Path = store.DB_PATH,
) -> TicketResolutionState:
    """Drive the state through the graph from `start` until pause or terminal.

    The input is the state as a dict, never a Command: on a thread with no
    checkpoint yet, LangGraph would feed a Command to START as data. On a
    thread that already exists, a dict input drops any pending task (a
    finished run, or an unanswered interrupt) and starts over at `start`.
    """
    _invoke(state.model_dump(), state.request_id, db_path, start=start)
    # Return what the runs table holds, not LangGraph's output: both runtimes
    # hand back the state the API would show.
    loaded = store.load_state(state.request_id, db_path=db_path)
    assert loaded is not None
    return loaded[0]


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
    """Continue a run from its last checkpoint. `next_node` (from the runs
    table) is only a fallback for a run with no checkpoint, for example one
    started under the plain runtime."""
    # .next names the node(s) LangGraph would run next: empty when the run
    # finished or has no checkpoint. invoke(None) means "no new input, carry
    # on from the last checkpoint". On a paused run that re-enters
    # await_approval and interrupt() pauses again: a restart approves nothing.
    if checkpoint_state(state.request_id, db_path).next:
        _invoke(None, state.request_id, db_path)
        loaded = store.load_state(state.request_id, db_path=db_path)
        assert loaded is not None
        return loaded[0]
    return run(state, start=next_node or "handle_failure", db_path=db_path)


def _resume_approval(
    state: TicketResolutionState, approved: bool, db_path: Path
) -> None:
    # A pending interrupt shows up as .next == (AWAIT_APPROVAL,). On this
    # branch the `state` argument is not used: LangGraph resumes its own
    # checkpointed copy, and _await_approval records the decision.
    if checkpoint_state(state.request_id, db_path).next == (AWAIT_APPROVAL,):
        _invoke(Command(resume=approved), state.request_id, db_path)
    else:  # no interrupt to answer (paused under the plain runtime)
        start = "execute_actions" if approved else "draft_customer_reply"
        run(state, start=start, db_path=db_path)


def resume_after_approval(
    state: TicketResolutionState,
    approved: bool,
    db_path: Path = store.DB_PATH,
) -> threading.Thread:
    """Answer the pending interrupt with the human's decision."""
    thread = threading.Thread(
        target=_resume_approval,
        args=(state, approved, db_path),
        daemon=True,
        name=state.request_id,
    )
    thread.start()
    return thread


def resume_stuck_runs(db_path: Path = store.DB_PATH) -> list[str]:
    """On startup: re-launch runs that died in a non-terminal state.

    The runs table finds them; the checkpointer knows where each one stopped.
    """
    resumed: list[str] = []
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
