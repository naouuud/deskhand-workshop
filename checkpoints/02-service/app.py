"""Deskhand service — FastAPI front door over the graph runner.

HTTP contract:
- GET  /tickets                                -> the shared dataset, for the picker
- POST /tickets {ticket_id | ticket_text [, customer_name]} -> {request_id}
- GET  /runs/{request_id}                      -> full persisted state (machine view)
- GET  /runs/{request_id}/events               -> SSE stream (Last-Event-ID replay)
- POST /runs/{request_id}/approve {approved}   -> resumes a paused run

The console at /console is a *client* of exactly these endpoints — it has no
privileged access. Anything it does, curl can do.

Run: uvicorn app:app --port 8000   (mock order API must be up on port 9000)
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator
from sse_starlette.sse import EventSourceResponse

import errors
import obslog
import runtime
from runtime import graph  # graph.py or graph_lg.py, picked by DESKHAND_RUNTIME
import store
from models import TERMINAL_STATUSES, TicketResolutionState

TICKETS_PATH = (
    Path(__file__).resolve().parents[2] / "shared" / "data" / "tickets.json"
)
CONSOLE_DIR = Path(__file__).resolve().parent / "console"
# The event stream polls the event log in SQLite; nothing pushes to it. The
# graph runs in another thread and stores every event before anyone sees it,
# so a live client and a reconnecting one read the same rows the same way.
# 0.25s is about the longest a client waits for any one event.
SSE_POLL_INTERVAL_S = 0.25


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    obslog.log_event("startup", "service.runtime", runtime=runtime.NAME)
    # Crash recovery, before the first request. A run whose process died
    # mid-flight is still in runs.db with a non-terminal status, and it
    # restarts here in a background thread. Runs parked at awaiting_approval
    # are left alone: they wait for POST /approve, not for a restart.
    resumed = graph.resume_stuck_runs()
    if resumed:
        obslog.log_event("startup", "recovery.resumed_runs", request_ids=resumed)
    yield


app = FastAPI(title="Deskhand — checkpoint 02", lifespan=lifespan)


class TicketRequest(BaseModel):
    """The body of POST /tickets, checked before any run exists: a bad body is
    a 422 and costs no model call. At least one of ticket_id and ticket_text;
    when both come, ticket_id wins and ticket_text is ignored."""

    ticket_id: str | None = None
    ticket_text: str | None = None
    # Optional, free text only: who the ticket is from, vouched for by the
    # client (as a mail gateway vouches for a From address). The ownership
    # rule then runs as it does for dataset tickets. Left out, the sender is
    # unknown and any refund or replacement stops at the approval gate. A
    # ticket_id always keeps the dataset's name: sending both is a 422.
    customer_name: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "TicketRequest":
        if not self.ticket_id and not self.ticket_text:
            raise ValueError("provide ticket_id or ticket_text")
        if self.customer_name is not None:
            self.customer_name = self.customer_name.strip() or None  # blank = unknown
        if self.ticket_id and self.customer_name:
            raise ValueError(
                "customer_name goes with ticket_text only; "
                "a ticket_id keeps the dataset's customer"
            )
        return self


class ApprovalRequest(BaseModel):
    # Yes or no, nothing else. The approver decides on the plan the run
    # already holds and cannot change its amount or actions from here.
    approved: bool


def _load_tickets() -> list[dict]:
    if not TICKETS_PATH.exists():
        raise HTTPException(500, f"tickets dataset not found at {TICKETS_PATH}")
    return json.loads(TICKETS_PATH.read_text(encoding="utf-8"))["tickets"]


def _load_ticket(ticket_id: str) -> dict:
    """The dataset record flattened for the model, plus the one field the
    business rules trust: who the ticket is from. The text carries the name
    too, but the model reads that; the rules read this."""
    for ticket in _load_tickets():
        if ticket["ticket_id"] == ticket_id:
            header = f"Ticket {ticket_id} from {ticket['customer_name']}"
            if ticket.get("order_id"):
                header += f" (order {ticket['order_id']})"
            body = "\n\n".join(
                f"[{m['from']}] {m['text']}" for m in ticket["messages"]
            )
            return {"text": f"{header}\n{body}", "customer_name": ticket["customer_name"]}
    raise HTTPException(404, f"unknown ticket_id {ticket_id}")


@app.get("/tickets")
def list_tickets() -> dict:
    """Catalogue for the console's ticket picker. Full text stays server-side;
    the picker only needs enough to choose."""
    return {
        "tickets": [
            {
                "ticket_id": t["ticket_id"],
                "customer_name": t["customer_name"],
                "order_id": t.get("order_id"),
                "channel": t.get("channel"),
                "tags": t.get("tags", []),
                "message_count": len(t["messages"]),
                "preview": t["messages"][0]["text"][:160],  # test_console pins the 160
            }
            for t in _load_tickets()
        ]
    }


@app.post("/tickets")
def submit_ticket(body: TicketRequest) -> dict:
    """Accept a ticket and answer at once with its request_id. The run itself
    happens in a background thread and takes seconds of model calls; the
    client follows it on GET /runs/{id}/events.

    The state row is saved before the thread starts. GET /runs/{id} and the
    event stream find the run straight away, and a crash before the first
    node still leaves a row that resume_stuck_runs restarts at ENTRY."""
    # 12 hex characters, short enough to read out in class. Every refund's
    # idempotency key is built from it (request_id:action index, nodes.py).
    request_id = uuid.uuid4().hex[:12]
    ticket_id = body.ticket_id or f"adhoc-{request_id}"
    if body.ticket_id:
        ticket = _load_ticket(body.ticket_id)
        ticket_text, customer_name = ticket["text"], ticket["customer_name"]
    else:  # free text: the sender is whoever the client vouches for, or unknown
        ticket_text, customer_name = body.ticket_text, body.customer_name
    # Two kinds of input. ticket_text is untrusted: the model reads it, and
    # some tickets are prompt injections. customer_name is what rules.py's
    # ownership check trusts, and it comes from the dataset or the client,
    # never from text the model reads.
    state = TicketResolutionState(
        request_id=request_id,
        ticket_id=ticket_id,
        ticket_text=ticket_text or "",
        customer_name=customer_name,
    )
    store.save_state(state, next_node=graph.ENTRY)
    obslog.log_event(request_id, "run.accepted", ticket_id=ticket_id)
    graph.run_in_thread(state)
    return {"request_id": request_id}


@app.get("/runs/{request_id}")
def get_run(request_id: str) -> dict:
    """The machine view: the whole persisted state and the node the run will
    continue from. It is the row the runner saves after every node, so it is
    exactly what a crash recovery would resume."""
    loaded = store.load_state(request_id)
    if loaded is None:
        raise HTTPException(404, "unknown request_id")
    state, next_node = loaded
    return {**state.model_dump(), "next_node": next_node}


@app.post("/runs/{request_id}/approve")
def approve_run(request_id: str, body: ApprovalRequest) -> dict:
    """The other half of the human approval gate. approval_gate parked the run
    at awaiting_approval; this records the decision and resumes the run in a
    background thread. It returns before any refund is made: the outcome
    arrives on the event stream."""
    loaded = store.load_state(request_id)
    if loaded is None:
        raise HTTPException(404, "unknown request_id")
    state, _ = loaded
    # 409 for a run that is not waiting: approving a finished run, or one
    # that never paused, is a client error, not a second chance to pay.
    # The check and the resume are not atomic. Two approvals sent close
    # together can both pass it, because the stored status changes only when
    # the resumed run saves its next node. The refund's idempotency key is
    # what keeps that to one payment.
    if state.status != "awaiting_approval":
        raise HTTPException(409, f"run is {state.status}, not awaiting_approval")

    state.approval_status = "approved" if body.approved else "rejected"
    obslog.log_event(request_id, "approval.decided", approved=body.approved)
    # Approved: execute the write actions. Rejected: skip execution, tell the
    # customer a human took over. The runtime decides how to continue: the
    # plain runner restarts at the right node, LangGraph resumes its interrupt.
    graph.resume_after_approval(state, body.approved)
    return {"request_id": request_id, "approval_status": state.approval_status}


def _log_stream_disconnected(request_id: str, last_event_id: int) -> None:
    """The client went away mid-stream. A fact to record, not a failure of the
    run: the run carries on in the store, and a reconnect with Last-Event-ID
    replays everything after `last_event_id`."""
    obslog.log_event(
        request_id,
        "stream.disconnected",
        internal_error_code=errors.STREAM_DISCONNECTED,
        last_event_id=last_event_id,
    )


@app.get("/runs/{request_id}/events")
async def run_events(request_id: str, request: Request) -> EventSourceResponse:
    """Server-Sent Events for one run: replay what the client missed, then
    follow the run live until it ends. The stream only reads. Closing it (a
    tab closed, a dropped network) leaves the run going."""
    if store.load_state(request_id) is None:
        raise HTTPException(404, "unknown request_id")
    # The browser's EventSource sends Last-Event-ID by itself when it
    # reconnects. Each event's id is its per-run sequence number in the
    # store, so replay is "events with seq > N". No header means from seq 1.
    last_seq = int(request.headers.get("Last-Event-ID") or 0)

    async def event_source():
        seq = last_seq
        try:
            while True:
                if await request.is_disconnected():
                    _log_stream_disconnected(request_id, seq)
                    return
                # SQLite calls block. to_thread keeps them off the event loop,
                # so one stream's query does not stall every other open stream.
                rows = await asyncio.to_thread(store.events_since, request_id, seq)
                for seq_, event, data in rows:
                    seq = seq_
                    # "id" becomes the SSE id: line, which the browser hands
                    # back as Last-Event-ID on its next reconnect.
                    yield {"id": str(seq), "event": event, "data": json.dumps(data)}
                # Close only after a poll that found nothing new. The status is
                # read after the events, so an event written between the two
                # reads would be lost if the status alone closed the stream. A
                # run at awaiting_approval is not terminal: the stream stays
                # open through the wait and carries the resumed run's events.
                loaded = await asyncio.to_thread(store.load_state, request_id)
                if loaded and loaded[0].status in TERMINAL_STATUSES and not rows:
                    return  # terminal event already delivered — close cleanly
                await asyncio.sleep(SSE_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            # sse-starlette cancels this generator when the client goes away,
            # usually before the is_disconnected() check above gets a chance.
            _log_stream_disconnected(request_id, seq)
            raise

    return EventSourceResponse(event_source())


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/console/")


if CONSOLE_DIR.is_dir():  # same origin as the API, so no CORS; no console/, no mount
    app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True), name="console")
