"""The bounded agent, offline: the same scripted model and fake order API as
test_agent_demo.py, now with the three layers around the loop.

What is under test is exactly what test_agent_demo.py shows the prebuilt agent
LETS the model do, now refused: a refund above the order total, a refund with
no lookup, a big refund with no human, a key the model chose, a loop that
never ends.
"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

import agent_bounded  # noqa: E402
import errors  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from test_agent_demo import _call, scripted  # noqa: E402

REQUEST_ID = "req-agent-0001"
TICKET = "My headphones (order O-48291) arrived cracked. Please refund me."


@pytest.fixture(autouse=True)
def _close_checkpoints():
    yield
    agent_bounded.close_all()


def _run(db_path, monkeypatch, *script: AIMessage | str, **kwargs) -> agent_bounded.Outcome:
    scripted(*script, monkeypatch=monkeypatch)
    return agent_bounded.run(TICKET, request_id=REQUEST_ID, db_path=db_path, **kwargs)


# Layer 3. The refund goes through, but under request_id:0, whatever key
# the model wrote. The model's key appears only in the log.
def test_small_refund_runs_with_a_derived_key(db_path, order_api, monkeypatch) -> None:
    out = _run(
        db_path, monkeypatch,
        _call("lookup_order", "c1", order_id="O-48291"),
        _call("issue_refund", "c2", order_id="O-48291", amount_usd=30.0,
              idempotency_key="made-up-by-the-model"),
        "Refunded $30.",
    )

    assert out.status == "completed"
    assert out.transcript.reply == "Refunded $30."
    # The POST happened, with the run's key, not the model's.
    assert list(order_api.refunds_by_key) == [f"{REQUEST_ID}:0"]
    assert order_api.refunds_by_key[f"{REQUEST_ID}:0"]["amount_usd"] == 30.0
    assert out.values["writes"] == 1
    assert any("the model wrote 'made-up-by-the-model'" in line for line in out.log)


# Layer 2, rule 1: the $10,000 injection that agent_demo.py lets through.
# The refusal goes back to the model as a tool error and no POST is made.
def test_rule_1_refuses_more_than_the_order_total(db_path, order_api, monkeypatch) -> None:
    out = _run(
        db_path, monkeypatch,
        _call("lookup_order", "c1", order_id="O-48291"),
        _call("issue_refund", "c2", order_id="O-48291", amount_usd=10_000.0,
              idempotency_key="k"),
        "I cannot refund that amount.",
    )

    assert out.status == "completed"
    assert order_api.refund_post_count == 0
    refused = [c for c in out.transcript.tool_calls if c.name == "issue_refund"]
    assert refused[0].error is True
    assert "exceeds the order total $79.99" in refused[0].result
    assert out.transcript.refunds == []


def test_no_lookup_no_refund(db_path, order_api, monkeypatch) -> None:
    """The model goes straight for the write. The gate has no order total to
    check against, so the answer is no, whatever the amount."""
    out = _run(
        db_path, monkeypatch,
        _call("issue_refund", "c1", order_id="O-48291", amount_usd=5.0,
              idempotency_key="k"),
        "Let me check the order first.",
    )

    assert order_api.refund_post_count == 0
    assert "was not looked up in this run" in out.transcript.tool_calls[0].result


# Layer 2, the approval gate. Above $50 the run parks inside gate with
# nothing paid, then pays exactly once, with the derived key, on approval.
def test_big_refund_parks_for_a_human_then_approved(db_path, order_api, monkeypatch) -> None:
    out = _run(
        db_path, monkeypatch,
        _call("lookup_order", "c1", order_id="O-48291"),
        _call("issue_refund", "c2", order_id="O-48291", amount_usd=79.99,
              idempotency_key="k"),
        "Your $79.99 refund has been issued.",
    )

    assert out.status == "awaiting_approval"
    assert out.paused["amount_usd"] == 79.99
    assert out.paused["order_total_usd"] == 79.99
    assert out.paused["limit_usd"] == 50.0
    assert order_api.refund_post_count == 0  # nothing moved yet
    snapshot = agent_bounded.checkpoint_state(REQUEST_ID, db_path)
    assert snapshot.next == ("gate",)

    out = agent_bounded.resume(REQUEST_ID, approved=True, db_path=db_path)

    assert out.status == "completed"
    assert list(order_api.refunds_by_key) == [f"{REQUEST_ID}:0"]
    assert any("a human approved it" in line for line in out.log)


# A rejection is not a crash: the model is told a human declined and
# writes the customer reply itself.
def test_big_refund_rejected_goes_back_to_the_model(db_path, order_api, monkeypatch) -> None:
    out = _run(
        db_path, monkeypatch,
        _call("lookup_order", "c1", order_id="O-48291"),
        _call("issue_refund", "c2", order_id="O-48291", amount_usd=79.99,
              idempotency_key="k"),
        "A manager has declined the refund; I have escalated your ticket.",
    )
    assert out.status == "awaiting_approval"

    out = agent_bounded.resume(REQUEST_ID, approved=False, db_path=db_path)

    assert out.status == "completed"
    assert order_api.refund_post_count == 0
    refused = [c for c in out.transcript.tool_calls if c.name == "issue_refund"]
    assert "a human declined" in refused[0].result
    assert out.transcript.reply.startswith("A manager has declined")


def test_crash_after_the_post_resumes_without_paying_twice(db_path, order_api, monkeypatch) -> None:
    """Lab part 3's murder, on the agent: the POST went out, the checkpoint
    did not. The resumed run re-runs run_tools with the same `writes`, so the
    same key, so the mock API returns the original refund."""
    scripted(
        _call("lookup_order", "c1", order_id="O-48291"),
        _call("issue_refund", "c2", order_id="O-48291", amount_usd=30.0,
              idempotency_key="k"),
        "Refunded $30.",
        monkeypatch=monkeypatch,
    )
    with pytest.raises(KeyboardInterrupt):
        agent_bounded.run(TICKET, request_id=REQUEST_ID, db_path=db_path,
                          crash_after_refund=True)

    assert order_api.refund_post_count == 1  # money moved
    assert agent_bounded.checkpoint_state(REQUEST_ID, db_path).next == ("run_tools",)

    out = agent_bounded.resume(REQUEST_ID, db_path=db_path)

    assert out.status == "completed"
    assert order_api.refund_post_count == 2  # the POST was repeated...
    assert list(order_api.refunds_by_key) == [f"{REQUEST_ID}:0"]  # ...and paid once
    assert out.values["writes"] == 1


def test_budget_stops_a_loop_the_model_would_not(db_path, order_api, monkeypatch) -> None:
    """A model that keeps looking the order up. The prebuilt agent would call
    it until the provider said no; this one stops at max_steps with a
    taxonomy code and a safe message."""
    lookups = [_call("lookup_order", f"c{i}", order_id="O-48291") for i in range(20)]
    out = _run(db_path, monkeypatch, *lookups, max_steps=3)

    assert out.status == "failed"
    assert out.values["internal_error_code"] == errors.BUDGET_EXCEEDED
    assert "routed to a human agent" in out.values["user_visible_error"]
    assert out.values["steps"] == 3
    assert sum(isinstance(m, AIMessage) for m in out.values["messages"]) == 3
    assert order_api.refund_post_count == 0


def test_tool_errors_go_back_to_the_model_as_text(db_path, order_api, monkeypatch) -> None:
    """A schema rejection or a 404 is a message to the model, not a crash:
    the same contract as the prebuilt agent."""
    out = _run(
        db_path, monkeypatch,
        _call("lookup_order", "c1", order_id="O-00000"),  # unknown order
        _call("issue_refund", "c2", order_id="O-48291", amount_usd=0,
              idempotency_key="k"),  # rejected by the schema (gt=0)...
        "I need a valid order number.",
    )

    assert out.status == "completed"
    assert order_api.refund_post_count == 0
    assert out.transcript.tool_calls[0].result == "null"
    assert out.transcript.tool_calls[1].error is True
    assert any("not looked up" in line or "rejected" in line for line in out.log)


# --no-write still counts the write (so keys keep their index) but never
# reaches the order API.
def test_no_write_records_instead_of_posting(db_path, order_api, monkeypatch) -> None:
    out = _run(
        db_path, monkeypatch,
        _call("lookup_order", "c1", order_id="O-48291"),
        _call("issue_refund", "c2", order_id="O-48291", amount_usd=20.0,
              idempotency_key="k"),
        "Done.",
        write=False,
    )

    assert out.status == "completed"
    assert order_api.refund_post_count == 0
    assert "R-DRYRUN-1" in out.transcript.refunds[0].result
    assert out.values["writes"] == 1


# The shape claim from the module docstring, read off the compiled graph:
# the three-node cycle, and the exit from call_model to END.
def test_the_graph_is_one_cycle() -> None:
    graph = agent_bounded.build().compile().get_graph()
    edges = {(e.source, e.target) for e in graph.edges}

    assert ("call_model", "gate") in edges
    assert ("gate", "run_tools") in edges
    assert ("run_tools", "call_model") in edges  # the loop the model drives
    assert ("call_model", "__end__") in edges
    # Every edge is a Command(goto=...), so Mermaid draws them dashed.
    assert "run_tools -.-> call_model" in graph.draw_mermaid()


# The CLI cannot resume a parked run without a decision, or answer a
# run that already finished.
def test_resume_refuses_the_wrong_kind_of_continue(db_path, order_api, monkeypatch) -> None:
    out = _run(
        db_path, monkeypatch,
        _call("lookup_order", "c1", order_id="O-48291"),
        _call("issue_refund", "c2", order_id="O-48291", amount_usd=79.99,
              idempotency_key="k"),
        "ok",
    )
    assert out.status == "awaiting_approval"
    with pytest.raises(SystemExit, match="awaiting approval"):
        agent_bounded.resume(REQUEST_ID, db_path=db_path)  # needs a decision

    agent_bounded.resume(REQUEST_ID, approved=False, db_path=db_path)
    with pytest.raises(SystemExit, match="nothing to resume"):
        agent_bounded.resume(REQUEST_ID, approved=True, db_path=db_path)
