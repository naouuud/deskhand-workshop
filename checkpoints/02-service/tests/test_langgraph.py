"""What LangGraph does for the run that graph.py did by hand: pause with
interrupt(), resume with Command(resume=...), and pick up a crashed run from
its checkpoint. Fully offline, the same fakes as test_recovery.py.
"""
from __future__ import annotations

import importlib
import sqlite3
import time

import pytest

pytest.importorskip("langgraph")

import graph_lg  # noqa: E402
import llm  # noqa: E402
import runtime  # noqa: E402
import store  # noqa: E402
from models import TriageResult  # noqa: E402
from test_recovery import (  # noqa: E402
    GOOD_PLAN,
    NO_USAGE,
    TRIAGE,
    crashing_stream_text,
    fake_stream_text,
    make_state,
)

REQUEST_ID = "req-recover01"
# $79.99 is over the $50 auto-approve limit, so the plan must ask for approval.
BIG_PLAN = GOOD_PLAN.model_copy(
    update={"refund_amount_usd": 79.99, "requires_approval": True}
)


@pytest.fixture(autouse=True)
def _close_checkpoints():
    yield
    graph_lg.close_all()


def _structured(plan):
    def fake(messages, response_model, **kwargs):
        if response_model is TriageResult:
            return TRIAGE.model_copy(deep=True), NO_USAGE
        return plan.model_copy(deep=True), NO_USAGE

    return fake


def _events(db_path) -> list[tuple[str, dict]]:
    return [(e, d) for _, e, d in store.events_since(REQUEST_ID, 0, db_path)]


def _count(db_path, event: str, **match) -> int:
    return sum(
        1
        for e, d in _events(db_path)
        if e == event and all(d.get(k) == v for k, v in match.items())
    )


@pytest.fixture()
def paused(monkeypatch, db_path, order_api):
    """A run stopped at the approval interrupt."""
    monkeypatch.setattr(llm, "structured_completion", _structured(BIG_PLAN))
    monkeypatch.setattr(llm, "stream_text", fake_stream_text)
    return graph_lg.run(make_state(), db_path=db_path)


def test_approval_gate_interrupts_once(paused, db_path, order_api) -> None:
    snapshot = graph_lg.checkpoint_state(REQUEST_ID, db_path)

    assert snapshot.next == ("await_approval",)
    assert len(snapshot.interrupts) == 1
    assert snapshot.interrupts[0].value["ticket_id"] == "T-1001"
    assert snapshot.interrupts[0].value["refund_amount_usd"] == 79.99
    # The approver sees the gap, not just the number the model chose.
    assert set(snapshot.interrupts[0].value) >= {
        "requested_refund_usd", "order_total_usd", "customer_match", "rationale"
    }
    assert snapshot.interrupts[0].value["order_total_usd"] == 79.99
    history = graph_lg.checkpoint_history(REQUEST_ID, db_path)
    assert history[0].next == ("await_approval",)  # newest first
    assert len(history) > len(paused.node_latency_ms)  # a checkpoint per step

    # The API's view (runs table) matches the plain runtime exactly.
    assert paused.status == "awaiting_approval"
    assert paused.approval_status == "pending"
    assert store.load_state(REQUEST_ID, db_path)[1] is None  # PAUSE is stored as no next node
    assert store.find_resumable(db_path=db_path) == []  # a human owns it now
    assert _count(db_path, "approval.required") == 1
    assert order_api.refund_post_count == 0


def test_approved_resume_refunds_once_without_duplicate_events(
    paused, db_path, order_api
) -> None:
    # It returns the worker thread; join it so the asserts see the finished run.
    graph_lg.resume_after_approval(paused, True, db_path=db_path).join(timeout=10)

    state = store.load_state(REQUEST_ID, db_path)[0]
    assert state.status == "completed"
    assert state.approval_status == "approved"
    assert order_api.refund_post_count == 1
    assert graph_lg.checkpoint_state(REQUEST_ID, db_path).next == ()
    # Resuming re-ran await_approval from the top, but nothing that emits
    # events lives before its interrupt(), so nothing was sent twice.
    assert _count(db_path, "approval.required") == 1
    assert _count(db_path, "run.status", status="awaiting_approval") == 1
    # await_approval emits no node events, so execute_actions is the next
    # node.started after approval_gate.
    started = [d["node"] for e, d in _events(db_path) if e == "node.started"]
    assert started[started.index("approval_gate") + 1] == "execute_actions"


def test_rejected_resume_skips_the_refund(paused, db_path, order_api) -> None:
    graph_lg.resume_after_approval(paused, False, db_path=db_path).join(timeout=10)

    state = store.load_state(REQUEST_ID, db_path)[0]
    assert state.status == "completed"
    assert state.approval_status == "rejected"
    assert order_api.refund_post_count == 0
    assert "execute_actions" not in state.node_latency_ms


def test_resume_without_an_answer_just_pauses_again(paused, db_path) -> None:
    # A restart re-enters await_approval, and interrupt() has no answer to
    # return, so the run pauses again. Restarting cannot approve a refund.
    graph_lg.resume(paused, None, db_path=db_path)

    assert graph_lg.checkpoint_state(REQUEST_ID, db_path).next == ("await_approval",)
    assert store.load_state(REQUEST_ID, db_path)[0].status == "awaiting_approval"
    assert _count(db_path, "approval.required") == 1


@pytest.fixture()
def crashed(monkeypatch, db_path, order_api):
    """A run killed inside draft_customer_reply, after the refund went out."""
    monkeypatch.setattr(llm, "structured_completion", _structured(GOOD_PLAN))
    monkeypatch.setattr(llm, "stream_text", crashing_stream_text)
    with pytest.raises(KeyboardInterrupt):
        graph_lg.run(make_state(), db_path=db_path)
    monkeypatch.setattr(llm, "stream_text", fake_stream_text)


def test_crash_leaves_the_checkpoint_before_the_crashed_node(
    crashed, db_path, order_api
) -> None:
    # durability="sync" saved the checkpoint before draft_customer_reply
    # began, so .next names the node that crashed.
    snapshot = graph_lg.checkpoint_state(REQUEST_ID, db_path)

    assert snapshot.next == ("draft_customer_reply",)
    assert snapshot.values["status"] == "executing"
    assert order_api.refund_post_count == 1


def test_resume_stuck_runs_continues_from_the_checkpoint(
    crashed, db_path, order_api
) -> None:
    assert graph_lg.resume_stuck_runs(db_path=db_path) == [REQUEST_ID]

    # resume_stuck_runs starts a thread and returns; poll until it finishes.
    deadline = time.monotonic() + 10
    while store.load_state(REQUEST_ID, db_path)[0].status != "completed":
        assert time.monotonic() < deadline, "resumed run did not complete"
        time.sleep(0.05)

    state = store.load_state(REQUEST_ID, db_path)[0]
    assert state.customer_reply_draft == "Hello, your refund is on its way."
    assert order_api.refund_post_count == 1  # execute_actions did not re-run


def test_checkpoints_live_next_to_runs_db(paused, db_path) -> None:
    lg_path = db_path.with_name("runs.langgraph.db")
    assert lg_path.exists()

    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "checkpoints" not in tables  # our runs.db stays ours


def test_runtime_switch(monkeypatch) -> None:
    monkeypatch.delenv("DESKHAND_RUNTIME", raising=False)
    assert runtime.load().__name__ == "graph"

    monkeypatch.setenv("DESKHAND_RUNTIME", "langgraph")
    assert runtime.load().__name__ == "graph_lg"
    assert importlib.reload(runtime).NAME == "langgraph"

    monkeypatch.setenv("DESKHAND_RUNTIME", "agent")
    with pytest.raises(ValueError, match="langgraph, plain"):
        runtime.load()

    monkeypatch.delenv("DESKHAND_RUNTIME")
    assert importlib.reload(runtime).NAME == "plain"
