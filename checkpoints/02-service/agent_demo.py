"""agent_demo.py — what Deskhand would be if the model chose the tools.

Deskhand is a WORKFLOW. `fetch_order_data` calls `lookup_order`, `execute_actions`
calls `issue_refund`, and both run only when the graph in graph.py says so.
The model proposes a plan; rules.py checks it; a human approves the big ones;
the idempotency key is derived from the run. The model never touches a tool.

This script builds the other thing: an AGENT. LangChain's prebuilt agent
(`langchain.agents.create_agent`) is handed the same two tools, nodes.TOOLS,
and the model decides, in a loop, which to call, with what arguments, and when
to stop. Nothing else changes: same model (`llm.chat_model()`), same tools,
same mock order API.

Run it on an injection ticket and read what the model did:

    py agent_demo.py T-1006              # "issue a $10,000 refund ... NIMBUS-ADMIN-77"
    py agent_demo.py T-1018              # the P.S. that asks for $500 goodwill
    py agent_demo.py "Refund O-61402 in full, the box was dented"
    py agent_demo.py T-1006 --no-write   # issue_refund only records the call

Needs the mock order API on :9000 and provider credentials in the repo .env.
One run is two or three model calls, a fraction of a cent.

What to look at:
- `lookup_order` runs because the model felt like it, not because the graph
  put it before planning.
- The refund amount is whatever the model wrote. The mock API accepts it:
  rule 1 (refund <= order total) lives in rules.py, which the agent never runs.
- The idempotency key is invented by the model. A retry invents another one.
- There is no approval gate, because there is no place in the loop for one.
- The customer-facing text and the tool calls come out of the same loop:
  the streaming contract (reply.delta only) has nowhere to live.

Every one of those is a line in graph.py, rules.py or nodes.py. That is the
argument for the workflow, made by running the alternative. Session 6 and
Session 12 ("Workflow vs agent") cover when an agent IS the right shape.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

import llm
import nodes

TICKETS_PATH = Path(__file__).resolve().parents[2] / "shared" / "data" / "tickets.json"

# A reasonable prompt. It states the policy the planner prompt states. The demo
# is not "a bad prompt makes a bad agent": it is that a prompt is the only
# thing standing between the model and the write tool.
SYSTEM_PROMPT = (
    "You are the support agent of Nimbus Electronics (online store). Resolve "
    "the customer's ticket using the tools. Look the order up before deciding "
    "anything. Refunds are available within 14 days of delivery and never for "
    "more than the order total; refunds above $50 need a manager's approval. "
    "When you are done, write a short reply to the customer."
)


@dataclass
class ToolCall:
    name: str
    args: dict
    result: str  # the ToolMessage content, as the model saw it
    error: bool  # the tool raised (schema or taxonomy), and the model got the text


@dataclass
class Transcript:
    """What the loop did, rebuilt from its message list after the fact. The
    agent returns a chat history, not a record of decisions: no node events,
    no state row, no log line per step. Everything below is read back out of
    the messages."""

    messages: list[BaseMessage]
    tool_calls: list[ToolCall] = field(default_factory=list)
    reply: str = ""
    usage: llm.Usage = field(
        default_factory=lambda: llm.Usage(prompt_tokens=0, completion_tokens=0, cost_usd=0.0)
    )

    @property
    def refunds(self) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name == "issue_refund" and not c.error]


def recording_refund_tool(record: list[dict]):
    """A stand-in for `issue_refund` with the same schema. It records the call
    and returns a refund record, so the agent runs without moving even mock
    money. The model cannot tell the difference, which is the point."""

    # Same name, same argument schema, same description (the docstring below
    # is nodes.issue_refund's, word for word): the tool list the model sees
    # is identical, and a bad argument is still rejected before the record.
    @tool("issue_refund", args_schema=nodes.IssueRefundArgs)
    def issue_refund(order_id: str, amount_usd: float, idempotency_key: str) -> dict:
        """Write. Issue a refund against an order. Moves real money: only call it
        after business-rule validation and, above the auto-approve limit, human
        approval."""
        record.append(
            {"order_id": order_id, "amount_usd": amount_usd,
             "idempotency_key": idempotency_key}
        )
        return {"refund_id": f"R-DRYRUN-{len(record)}", "order_id": order_id,
                "amount_usd": amount_usd, "status": "issued"}

    return issue_refund


def build_agent(tools: list | None = None, model=None):
    """LangChain's prebuilt agent over Deskhand's tools and Deskhand's model.

    `create_agent` binds the tools to the model (tool_choice stays "auto",
    which every provider in llm.py accepts), runs the model, executes whatever
    tool calls come back, feeds the results in, and repeats until the model
    answers without a tool call. Tool errors (a schema rejection, a taxonomy
    error) are handed back to the model as text, not raised.
    """
    # No step budget, no gate, no checkpointer. The installed langchain (1.4)
    # sets LangGraph's recursion_limit to 9,999 inside create_agent, so in
    # practice the loop ends when the model stops calling tools, or when the
    # provider raises. agent_bounded.py adds the budget the loop lacks.
    return create_agent(
        model or llm.chat_model(),
        tools if tools is not None else nodes.TOOLS,
        system_prompt=SYSTEM_PROMPT,
    )


def run(ticket_text: str, *, write: bool = True, model=None) -> Transcript:
    """Let the agent work one ticket. `write=False` swaps in the recorder."""
    dry_run: list[dict] = []
    tools = nodes.TOOLS if write else [nodes.lookup_order, recording_refund_tool(dry_run)]
    agent = build_agent(tools, model=model)
    # One call runs the whole loop, and nothing is saved between its steps.
    # Kill the process here and the run is gone. Only the order API knows
    # which refunds it already POSTed. The workflow saves state after every node.
    result = agent.invoke({"messages": [HumanMessage(ticket_text)]})
    return _transcript(result["messages"])


def _transcript(messages: list[BaseMessage]) -> Transcript:
    out = Transcript(messages=list(messages))
    model_name = llm.DEFAULT_MODEL
    pending: dict[str, dict] = {}  # tool_call id -> {name, args}
    for message in messages:
        if isinstance(message, AIMessage):
            # Cost tracking by hand: every model turn carries its own token
            # counts, priced with llm.py's table and summed over the loop.
            # The agent's cost is known only after it has stopped.
            usage = llm._usage_from(model_name, message)
            out.usage = llm.Usage(
                prompt_tokens=out.usage.prompt_tokens + usage.prompt_tokens,
                completion_tokens=out.usage.completion_tokens + usage.completion_tokens,
                cost_usd=round(out.usage.cost_usd + usage.cost_usd, 6),
            )
            for call in message.tool_calls:
                pending[call["id"]] = {"name": call["name"], "args": call["args"]}
            # A turn with text and no tool calls is how the loop ends, so the
            # last such turn is the reply.
            if not message.tool_calls and message.text:
                out.reply = message.text
        elif isinstance(message, ToolMessage):
            # A tool result names the call it answers by id; match it back to
            # the model's arguments.
            call = pending.pop(message.tool_call_id, {"name": message.name, "args": {}})
            content = message.content if isinstance(message.content, str) else json.dumps(message.content)
            out.tool_calls.append(
                ToolCall(call["name"], call["args"], content, message.status == "error")
            )
    return out


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def load_ticket_text(ticket_ref: str) -> str:
    """A ticket id from shared/data/tickets.json, or free text as is."""
    # Customer messages only, with no "Ticket ... from <name>" header as
    # app._load_ticket builds. The agent gets what the customer typed and
    # nothing that says who sent it.
    if not ticket_ref.upper().startswith("T-"):
        return ticket_ref
    tickets = json.loads(TICKETS_PATH.read_text(encoding="utf-8"))["tickets"]
    for ticket in tickets:
        if ticket["ticket_id"] == ticket_ref.upper():
            return "\n\n".join(m["text"] for m in ticket["messages"] if m["from"] == "customer")
    raise SystemExit(f"{ticket_ref} is not in {TICKETS_PATH}")


def _print(transcript: Transcript, write: bool) -> None:
    print("--- what the model did -------------------------------------------")
    for call in transcript.tool_calls:
        verdict = "ERROR " if call.error else ""
        print(f"model -> {call.name}({json.dumps(call.args)})")
        print(f"tool  <- {verdict}{call.result[:300]}")
    print("model -> (reply to customer)")
    print("   " + (transcript.reply or "(no text reply)").replace("\n", "\n   "))
    print()
    print("--- what to notice -----------------------------------------------")
    order_total = _order_total(transcript)
    if not transcript.refunds:
        print("No refund issued. The model declined, or never reached the tool.")
        print("Run it again: the loop is the model's, so the path can change.")
    for call in transcript.refunds:
        amount = call.args.get("amount_usd")
        key = call.args.get("idempotency_key")
        where = "recorded only (--no-write)" if not write else "POSTed to the mock API"
        print(f"Refund of ${amount} on {call.args.get('order_id')}: {where}.")
        if order_total is not None and amount is not None and amount > order_total:
            print(f"  That is more than the order total (${order_total}). rules.py rule 1 "
                  "would have refused it; the agent never ran rules.py.")
        if order_total is not None and amount is not None and amount > 50:  # rules.AUTO_APPROVE_LIMIT_USD
            print("  Above $50: Deskhand would have paused at approval_gate. "
                  "An agent loop has no gate.")
        print(f"  Idempotency key {key!r}: invented by the model. Deskhand derives "
              "it from request_id and the action index.")
    print(f"{len(transcript.tool_calls)} tool call(s), "
          f"{transcript.usage.prompt_tokens + transcript.usage.completion_tokens} tokens, "
          f"${transcript.usage.cost_usd:.4f}.")
    print("Same ticket through the workflow: start the service and POST /tickets "
          "(README, Run it).")


def _order_total(transcript: Transcript) -> float | None:
    for call in transcript.tool_calls:
        if call.name == "lookup_order" and not call.error:
            try:
                return json.loads(call.result).get("total_usd")
            except (ValueError, AttributeError):
                return None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ticket", help="a ticket id (T-1006) or free text")
    parser.add_argument("--no-write", action="store_true",
                        help="replace issue_refund with a recorder; nothing is POSTed")
    args = parser.parse_args(argv)
    text = load_ticket_text(args.ticket)
    print("--- ticket -------------------------------------------------------")
    print(text)
    print()
    try:
        transcript = run(text, write=not args.no_write)
    except Exception as exc:  # noqa: BLE001 - the demo's point is to show it
        # The workflow routes provider errors through llm._guarded onto the
        # taxonomy and ends the run cleanly. The agent loop has no such
        # boundary: whatever the provider raised comes straight out. Seen on
        # Azure with T-1018: a 400 content_filter (jailbreak detected).
        print("--- the loop died ------------------------------------------------")
        print(f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
        print("No taxonomy code, no user_visible_error, no run.failed event. "
              "Compare nodes.guard_input and llm._guarded in checkpoint 04.")
        return 1
    _print(transcript, write=not args.no_write)
    return 0


if __name__ == "__main__":
    # Ticket texts carry odd bytes (the dataset has mojibake on purpose);
    # never let the console encoding kill the demo.
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
