"""agent_bounded.py — the agent from agent_demo.py, with the workflow's three
layers put back around the loop.

agent_demo.py hands the model the two tools and lets it drive. Read its
transcript and three things are missing, each of them a line somewhere in
graph.py, rules.py or nodes.py:

    agent_demo.py (the model's loop)        agent_bounded.py (this file)
    runs until the model stops              a step budget: MAX_STEPS model calls, then BUDGET_EXCEEDED
    issue_refund runs when the model says   a gate before every write: rule 1 (refund <= order total,
                                            and only for an order this run looked up), then above the
                                            $50 limit interrupt() parks the run for a human
    the key is whatever the model wrote     the key is request_id:n, derived from the run; the model's
                                            value is overwritten

The loop is still the model's: it chooses which tool, with what arguments,
and when it is done. That is what makes it an agent. The three layers are
the same three the workflow has (Session 5, "Three layers between the model
and your money"), applied to a loop whose shape is decided at runtime.

It is a LangGraph graph with one cycle:

    START -> call_model -> gate -> run_tools -> call_model -> ... -> END

Compare it with graph_lg.py: same StateGraph, same interrupt(), same
SqliteSaver (runs.agent.db next to runs.db), same thread_id = request_id.
What differs is who draws the edges. In graph_lg.py every edge is in
DESTINATIONS; here the only real decision, "which tool next", is the
model's, and the graph only guarantees what happens around it.

    py agent_bounded.py T-1004                        # $45 keyboard: looked up, under $50, refunded once
    py agent_bounded.py T-1006                        # $10,000: if the model calls, rule 1 refuses; no human needed
    py agent_bounded.py "Refund O-48291 in full, the box was dented"   # $79.99: parks. Then:
    py agent_bounded.py --approve <request_id>        #   ...or --reject
    py agent_bounded.py T-1004 --crash-after-refund   # dies after the POST, before the checkpoint. Then:
    py agent_bounded.py --resume <request_id>         #   run_tools re-runs, same key, one refund
    py agent_bounded.py T-1004 --max-steps 2          # not enough calls to finish: BUDGET_EXCEEDED
    py agent_bounded.py T-1004 --no-write             # issue_refund is recorded, never POSTed
    py agent_bounded.py --draw                        # the graph as Mermaid: one cycle

Needs the mock order API on :9000 and provider credentials in the repo .env.
One run is two to four model calls, a fraction of a cent.

Still true, and still the reason Deskhand is a workflow: the path is the
model's, so cost, latency and the set of things that can go wrong are
bounded only by what these layers bound. A workflow bounds them by
construction. Reach for this shape when you cannot draw the graph.

Also still true: the gate runs two checks, rules.py runs more. Nothing here
asks whether the ticket's author owns the order (the laundered refund,
checkpoint 02 README). Every rule you leave out of the gate is a rule the
model is free to skip.
"""
from __future__ import annotations

import argparse
import json
import operator
import sqlite3
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, StateSnapshot, interrupt

import errors
import llm
import nodes
import rules
import store
from agent_demo import SYSTEM_PROMPT, Transcript, _transcript, load_ticket_text

# Layer 1, the step budget. A loop the model drives has no end of its own
# making, so the code sets one. Cost is not capped directly: it is tracked in
# the state. Every model call resends the whole growing history, so capping
# the calls is what caps cost and latency.
MAX_STEPS = 6  # model calls per run; a model call is a step, a tool call is not

# agent_demo.py's prompt tells the model that big refunds need a manager, and
# its loop has nowhere to ask one, so the model asks the customer instead
# (T-1001 in the README table). Here the gate asks, so the model is told to
# go ahead and call: the decision has moved from the prompt into code.
BOUNDED_PROMPT = SYSTEM_PROMPT + (
    " You do not ask for approval yourself: when a refund is due, call "
    "issue_refund with the amount you decided. The system checks it against "
    "policy, asks a manager when the amount needs one, and tells you the outcome."
)


# ---------------------------------------------------------------------------
# State: a TypedDict, not a Pydantic model. `messages` and `log` are append-only
# channels (a reducer merges each node's additions), everything else is
# overwritten. graph_lg.py returns the whole TicketResolutionState from every
# node; here a node returns only what it changed.
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    request_id: str
    messages: Annotated[list[BaseMessage], add_messages]
    log: Annotated[list[str], operator.add]  # what the layers did, in order
    steps: int  # model calls so far
    max_steps: int
    writes: int  # refunds issued so far: the index in the idempotency key
    order_id: str | None  # the order this run looked up (rule 1 needs it)
    order_total_usd: float | None
    # gate decides and run_tools acts, in two nodes, so interrupt() can sit in
    # a node with no side effects before it. The verdicts carry the gate's
    # decisions across the checkpoint between the two.
    verdicts: dict[str, str | None]  # tool_call id -> None (allowed) or the refusal
    write: bool  # False: issue_refund is recorded, never POSTed
    status: str  # running | completed | failed (awaiting_approval is read off the interrupt)
    internal_error_code: str | None
    user_visible_error: str | None
    reply: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


@dataclass
class RunContext:
    """Per-invocation context (graph_lg.RunContext does the same job). Not
    checkpointed, so a lab hook set on one invoke is gone on the resume."""

    crash_after_refund: bool = False  # die after the refund POST, before the checkpoint


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def _last_ai(state: AgentState) -> AIMessage:
    for message in reversed(state["messages"]):
        if isinstance(message, AIMessage):
            return message
    raise RuntimeError("no model turn yet")


def _failed(state: AgentState, code: str, line: str) -> Command:
    """End the run as a typed failure: a taxonomy code plus the safe message
    errors.py keeps for it, the same pair a failed workflow run ends with."""
    return Command(
        update={
            "status": "failed",
            "internal_error_code": code,
            "user_visible_error": errors.user_message_for(code),
            "log": [line],
        },
        goto=END,
    )


def call_model(state: AgentState) -> Command:
    """Layer 1, the budget. Then one model turn: tools bound for their
    schemas only; nothing runs here."""
    # Checked before the call, so max_steps is the number of model calls that
    # happen, never one more.
    if state["steps"] >= state["max_steps"]:
        return _failed(
            state,
            errors.BUDGET_EXCEEDED,
            f"budget: {state['steps']} model calls, the limit is "
            f"{state['max_steps']}; the loop stops here, not when the model says so",
        )
    # The tool allow-list: the model is shown these two tools and no others.
    # A call to any other name comes back from run_tools as an error message
    # and never executes.
    model = llm.chat_model().bind_tools(nodes.TOOLS)
    history = [SystemMessage(BOUNDED_PROMPT), *state["messages"]]
    try:
        # Same boundary as every workflow node: provider errors land on the
        # taxonomy and the run ends with a user-visible message, instead of
        # the raw exception agent_demo.py prints for T-1018 on Azure.
        ai: AIMessage = llm._guarded(lambda: model.invoke(history))
    except errors.DeskhandError as exc:
        return _failed(
            state, exc.internal_error_code, f"model: {exc.internal_error_code}: {exc}"
        )
    usage = llm._usage_from(llm.DEFAULT_MODEL, ai)
    step = state["steps"] + 1
    update: dict = {
        "messages": [ai],
        "steps": step,
        "prompt_tokens": state["prompt_tokens"] + usage.prompt_tokens,
        "completion_tokens": state["completion_tokens"] + usage.completion_tokens,
        "cost_usd": round(state["cost_usd"] + usage.cost_usd, 6),
        "log": [
            f"model call {step}/{state['max_steps']}: "
            + (", ".join(c["name"] for c in ai.tool_calls) if ai.tool_calls else "done, replied")
        ],
    }
    # The one branch the model owns: tool calls mean another lap, text alone
    # means done. Every other edge in this graph is fixed.
    if ai.tool_calls:
        return Command(update=update, goto="gate")
    update.update({"status": "completed", "reply": ai.text or ""})
    return Command(update=update, goto=END)


def gate(state: AgentState) -> Command:
    """Layer 2. Every issue_refund the model asked for is checked before it
    runs: rule 1 from rules.py (the order was looked up, the amount is within
    its total), then the approval limit. A refund above it calls interrupt(),
    which parks the run; Command(resume=True|False) restarts this node from
    the top with the decision. Nothing before interrupt() has side effects,
    for the same reason await_approval is its own node in graph_lg.py."""
    verdicts: dict[str, str | None] = {}
    log: list[str] = []
    for call in _last_ai(state).tool_calls:
        if call["name"] != "issue_refund":
            # Only the write is gated. lookup_order is a read and passes.
            verdicts[call["id"]] = None
            continue
        args = call["args"]
        order_id = args.get("order_id")
        total = state["order_total_usd"]
        try:
            amount = float(args.get("amount_usd"))
        except (TypeError, ValueError):
            amount = None
        reason: str | None = None
        # state["order_id"] is set by run_tools on an earlier lap, and holds
        # only the last order looked up. A refund asked for in the same model
        # turn as its order's first lookup is refused: the lookup has not run
        # yet. The model has to look, read the result, then ask.
        # These two checks restate rule 1 here; rules.py itself is not called,
        # so rule 1's ownership half is not checked (see the module docstring).
        # Each call is checked on its own. Two $45 refunds on a $45 order both
        # pass, and the second gets its own key (request_id:1): the key stops a
        # replay from paying twice, not a model that asks twice.
        if order_id != state["order_id"] or total is None:
            reason = (
                f"refused: {order_id} was not looked up in this run (or the lookup "
                "had no total). Call lookup_order first; never refund an order you "
                "could not see."
            )
        elif amount is None or amount > total:
            reason = (
                f"refused: ${amount} exceeds the order total ${total} (rule 1, "
                "rules.py). No refund above the order total, whoever asks."
            )
        elif amount > rules.AUTO_APPROVE_LIMIT_USD:
            # The human approval gate. On the first pass interrupt() stops the
            # run and checkpoints it with this payload. On resume the node runs
            # again from the top and interrupt() returns the human's answer.
            decision = interrupt(
                {
                    "request_id": state["request_id"],
                    "order_id": order_id,
                    "amount_usd": amount,
                    "order_total_usd": total,
                    "limit_usd": rules.AUTO_APPROVE_LIMIT_USD,
                    "model_said": _last_ai(state).text or "",
                }
            )
            if decision:
                log.append(f"gate: ${amount} on {order_id} is above ${rules.AUTO_APPROVE_LIMIT_USD:.0f}; a human approved it")
            else:
                reason = f"refused: a human declined the ${amount} refund on {order_id}"
        else:
            log.append(f"gate: ${amount} on {order_id} passes rule 1 and is under the ${rules.AUTO_APPROVE_LIMIT_USD:.0f} limit")
        verdicts[call["id"]] = reason
        if reason:
            log.append("gate: " + reason)
    return Command(update={"verdicts": verdicts, "log": log}, goto="run_tools")


def run_tools(state: AgentState, runtime: Runtime[RunContext]) -> Command:
    """Execute what the gate allowed. Layer 3 lives here: the idempotency key
    on a refund is request_id:n, whatever the model wrote. A refused call goes
    back to the model as an error message, exactly as a schema rejection
    does in agent_demo.py; the model decides what to do about it."""
    out: list[BaseMessage] = []
    log: list[str] = []
    writes = state["writes"]
    order_id, total = state["order_id"], state["order_total_usd"]
    crash = False
    paid = None  # the last refund id the API returned, for the crash message
    for call in _last_ai(state).tool_calls:
        reason = state["verdicts"].get(call["id"])
        if reason:
            out.append(ToolMessage(reason, tool_call_id=call["id"], name=call["name"], status="error"))
            continue
        # A copy. The model's message keeps the arguments it wrote, key
        # included, in the history and the checkpoint; only this call sees ours.
        args = dict(call["args"])
        try:
            if call["name"] == "lookup_order":
                result = nodes.lookup_order.invoke(args)
                if isinstance(result, dict):
                    order_id = result.get("order_id")
                    total = result.get("total_usd")  # None under the "inconsistent" chaos mode
                    log.append(f"tool: looked up {order_id}, total ${total}")
                else:
                    log.append(f"tool: {args.get('order_id')} not found")
            elif call["name"] == "issue_refund":
                # Layer 3, the idempotency key. `writes` is checkpointed state,
                # so a resume after a crash rebuilds the same key and the order
                # API hands back the original refund instead of paying again.
                key = f"{state['request_id']}:{writes}"
                log.append(f"key: the model wrote {args.get('idempotency_key')!r}; the run uses {key!r}")
                args["idempotency_key"] = key
                if state["write"]:
                    result = nodes.issue_refund.invoke(args)
                else:
                    nodes.IssueRefundArgs(**args)  # the schema still applies
                    result = {"refund_id": f"R-DRYRUN-{writes + 1}", "order_id": args["order_id"],
                              "amount_usd": args["amount_usd"], "status": "recorded"}
                writes += 1  # only after the call returned: a rejected refund keeps its index
                log.append(f"tool: refund {result.get('refund_id')} for ${args['amount_usd']} on {args['order_id']}")
                crash = runtime.context.crash_after_refund
                paid = result.get("refund_id")
            else:
                raise errors.DeskhandError(errors.INTERNAL_UNEXPECTED, f"unknown tool {call['name']}")
            out.append(ToolMessage(json.dumps(result), tool_call_id=call["id"], name=call["name"]))
        except errors.DeskhandError as exc:
            log.append(f"tool: {call['name']} failed with {exc.internal_error_code}")
            out.append(ToolMessage(f"{exc.internal_error_code}: {exc}", tool_call_id=call["id"],
                                   name=call["name"], status="error"))
        except Exception as exc:  # noqa: BLE001 - a schema rejection, back to the model as text
            log.append(f"tool: {call['name']} rejected the arguments ({type(exc).__name__})")
            out.append(ToolMessage(f"invalid arguments: {str(exc)[:400]}", tool_call_id=call["id"],
                                   name=call["name"], status="error"))
    if crash:
        # The POST happened; this node's checkpoint has not been written. A
        # resume re-runs run_tools with the same `writes`, so the same key.
        # KeyboardInterrupt, like a Ctrl+C: main() catches it and prints the
        # --resume command, and the offline test catches it in-process, which
        # nodes.py's os._exit(1) would not allow.
        raise KeyboardInterrupt(
            f"--crash-after-refund: killed after refund {paid} was paid, before the checkpoint"
        )
    # Every result goes back to the model as a ToolMessage, refusals included,
    # and the loop returns to call_model. verdicts is cleared for the next lap.
    return Command(
        update={"messages": out, "log": log, "writes": writes, "order_id": order_id,
                "order_total_usd": total, "verdicts": {}},
        goto="call_model",
    )


def build() -> StateGraph:
    """Three nodes and one cycle. Each node routes with Command(goto=...);
    `destinations` only declares where it may go, so --draw can show the edges."""
    builder = StateGraph(AgentState, context_schema=RunContext)
    builder.add_node("call_model", call_model, destinations=("gate", END))
    builder.add_node("gate", gate, destinations=("run_tools",))
    builder.add_node("run_tools", run_tools, destinations=("call_model",))
    builder.add_edge(START, "call_model")
    return builder


# ---------------------------------------------------------------------------
# One compiled graph + checkpointer per database (the graph_lg.py pattern)
# ---------------------------------------------------------------------------

_compiled_graphs: dict[Path, tuple[CompiledStateGraph, sqlite3.Connection]] = {}
_lock = threading.Lock()


def checkpoint_path(db_path: Path = store.DB_PATH) -> Path:
    """runs.db -> runs.agent.db, in the same folder."""
    return Path(db_path).with_suffix(".agent.db")


def _compiled(db_path: Path) -> CompiledStateGraph:
    key = Path(db_path).resolve()
    with _lock:
        if key not in _compiled_graphs:
            conn = sqlite3.connect(str(checkpoint_path(key)), check_same_thread=False)
            _compiled_graphs[key] = (build().compile(checkpointer=SqliteSaver(conn)), conn)
        return _compiled_graphs[key][0]


def close_all() -> None:
    """Close every checkpoint connection. For tests; never during a run."""
    with _lock:
        for _, conn in _compiled_graphs.values():
            conn.close()
        _compiled_graphs.clear()


def _config(request_id: str) -> dict:
    return {"configurable": {"thread_id": request_id}}


def checkpoint_state(request_id: str, db_path: Path = store.DB_PATH) -> StateSnapshot:
    """LangGraph's view of a run: .values, .next, .interrupts."""
    return _compiled(db_path).get_state(_config(request_id))


# ---------------------------------------------------------------------------
# Run, resume, read
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    request_id: str
    status: str  # running | awaiting_approval | completed | failed
    values: dict
    transcript: Transcript
    paused: dict | None  # the interrupt payload while awaiting approval

    @property
    def log(self) -> list[str]:
        return list(self.values.get("log", []))


def outcome(request_id: str, db_path: Path = store.DB_PATH) -> Outcome:
    snapshot = checkpoint_state(request_id, db_path)
    values = dict(snapshot.values)
    paused = snapshot.interrupts[0].value if snapshot.interrupts else None
    status = "awaiting_approval" if paused else values.get("status", "running")
    return Outcome(request_id, status, values, _transcript(values.get("messages", [])), paused)


def run(
    ticket_text: str,
    *,
    request_id: str | None = None,
    write: bool = True,
    max_steps: int | None = None,
    crash_after_refund: bool = False,
    db_path: Path = store.DB_PATH,
) -> Outcome:
    """Let the bounded agent work one ticket until it finishes, fails, or
    parks at the gate. Returns the run's outcome either way."""
    request_id = request_id or uuid.uuid4().hex[:12]
    state: AgentState = {
        "request_id": request_id,
        "messages": [HumanMessage(ticket_text)],
        "log": [],
        "steps": 0,
        "max_steps": max_steps or MAX_STEPS,
        "writes": 0,
        "order_id": None,
        "order_total_usd": None,
        "verdicts": {},
        "write": write,
        "status": "running",
        "internal_error_code": None,
        "user_visible_error": None,
        "reply": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
    }
    _compiled(db_path).invoke(
        state,
        _config(request_id),
        context=RunContext(crash_after_refund=crash_after_refund),
        durability="sync",  # checkpoint before the next node starts, not after
    )
    return outcome(request_id, db_path)


def resume(
    request_id: str, *, approved: bool | None = None, db_path: Path = store.DB_PATH
) -> Outcome:
    """Continue a run from its checkpoint. `approved` answers a pending
    interrupt; None resumes a run that crashed (re-running the node it died in)."""
    snapshot = checkpoint_state(request_id, db_path)
    if not snapshot.next:
        raise SystemExit(f"{request_id} has nothing to resume (status {snapshot.values.get('status')!r})")
    if snapshot.interrupts and approved is None:
        raise SystemExit(f"{request_id} is awaiting approval: pass --approve or --reject")
    if approved is not None and not snapshot.interrupts:
        raise SystemExit(f"{request_id} is not awaiting approval; use --resume")
    # Command(resume=...) answers the interrupt in gate. None means "carry on
    # from the last checkpoint": LangGraph re-runs the node that never finished.
    payload = Command(resume=approved) if approved is not None else None
    _compiled(db_path).invoke(
        payload, _config(request_id), context=RunContext(), durability="sync"
    )
    return outcome(request_id, db_path)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _print(out: Outcome) -> None:
    print("--- what the model did -------------------------------------------")
    for call in out.transcript.tool_calls:
        verdict = "ERROR " if call.error else ""
        print(f"model -> {call.name}({json.dumps(call.args)})")
        print(f"tool  <- {verdict}{call.result[:300]}")
    if out.status == "completed":
        print("model -> (reply to customer)")
        print("   " + (out.transcript.reply or "(no text reply)").replace("\n", "\n   "))
    print()
    print("--- what the layers did ------------------------------------------")
    for line in out.log:
        print("  " + line)
    print()
    values = out.values
    if out.status == "awaiting_approval":
        p = out.paused or {}
        print(f"PARKED at the gate: ${p.get('amount_usd')} on {p.get('order_id')} "
              f"(order total ${p.get('order_total_usd')}, limit ${p.get('limit_usd')}).")
        print("The run is in runs.agent.db and survives a restart. Decide:")
        print(f"   py agent_bounded.py --approve {out.request_id}")
        print(f"   py agent_bounded.py --reject  {out.request_id}")
    elif out.status == "failed":
        print(f"FAILED: {values.get('internal_error_code')}")
        print(f"   user sees: {values.get('user_visible_error')}")
    else:
        print(f"{out.status.upper()}.")
    print(f"request {out.request_id}: {values.get('steps')}/{values.get('max_steps')} model calls, "
          f"{values.get('writes')} refund(s), "
          f"{values.get('prompt_tokens', 0) + values.get('completion_tokens', 0)} tokens, "
          f"${values.get('cost_usd', 0.0):.4f}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ticket", nargs="?", help="a ticket id (T-1004) or free text")
    parser.add_argument("--no-write", action="store_true",
                        help="record refunds instead of POSTing them")
    parser.add_argument("--max-steps", type=int, default=None,
                        help=f"model calls allowed per run (default {MAX_STEPS})")
    parser.add_argument("--crash-after-refund", action="store_true",
                        help="lab hook: die right after the refund POST, before the checkpoint")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--approve", metavar="REQUEST_ID", help="answer a parked run: yes")
    group.add_argument("--reject", metavar="REQUEST_ID", help="answer a parked run: no")
    group.add_argument("--resume", metavar="REQUEST_ID", help="continue a crashed run")
    group.add_argument("--draw", action="store_true", help="print the graph as Mermaid")
    args = parser.parse_args(argv)

    if args.draw:
        print(build().compile().get_graph().draw_mermaid())
        return 0
    if args.approve or args.reject or args.resume:
        request_id = args.approve or args.reject or args.resume
        approved = True if args.approve else (False if args.reject else None)
        out = resume(request_id, approved=approved)
        _print(out)
        return 0 if out.status != "failed" else 1
    if not args.ticket:
        parser.error("give a ticket id or text, or --approve/--reject/--resume/--draw")
    text = load_ticket_text(args.ticket)
    print("--- ticket -------------------------------------------------------")
    print(text)
    print()
    request_id = uuid.uuid4().hex[:12]
    try:
        out = run(text, request_id=request_id, write=not args.no_write,
                  max_steps=args.max_steps, crash_after_refund=args.crash_after_refund)
    except KeyboardInterrupt as exc:
        print(f"--- crashed: {exc} ---")
        print("The refund POST went out; the checkpoint did not. Resume and watch the key:")
        print(f"   py agent_bounded.py --resume {request_id}")
        return 3
    _print(out)
    return 0 if out.status != "failed" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
