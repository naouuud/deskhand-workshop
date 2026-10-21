"""CLI: triage every ticket in shared/data/tickets.json and print a summary.

Run it with `py main.py` from this folder. Paths resolve relative to this
file, so it runs from any working directory.

This file is the "outside world" of the checkpoint: it loads tickets, calls
triage.classify_ticket once per ticket and shows the results. All the AI work
happens in triage.py and llm.py.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from dotenv import load_dotenv  # reads KEY=value lines from .env into os.environ
from rich.console import Console  # rich: pretty terminal output (tables, colors)
from rich.table import Table

import errors
import obslog
import triage

# parents[2]: main.py -> 01-triage -> checkpoints -> repo root. Anchored to
# this file, not the current directory, unlike v1's "../../shared" glob.
REPO_ROOT = Path(__file__).resolve().parents[2]
TICKETS_PATH = REPO_ROOT / "shared" / "data" / "tickets.json"


def main() -> None:
    """Triage every shared ticket, one request_id each, and print a table.

    Costs real money: at least one small call per ticket, two when a repair
    is needed. One ticket failing does not stop the batch: its row shows the
    error code and the loop moves on.
    """
    # llm.py already called load_dotenv() at import (it searches upward from
    # its own folder) and chose the provider then. This explicit load does not
    # override anything already set; it makes the .env location visible.
    load_dotenv(REPO_ROOT / ".env")
    tickets = json.loads(TICKETS_PATH.read_text(encoding="utf-8"))["tickets"]

    console = Console()
    table = Table(title=f"Deskhand triage — {len(tickets)} tickets")
    for column in ("ticket", "category", "priority", "sentiment", "confidence", "cost"):
        table.add_column(column)

    total_cost = 0.0
    failures = 0
    for ticket in tickets:
        # One id per run, written on every log line the run produces. The
        # table does not show it: find the ticket_id in logs/runs.jsonl, then
        # grep its request_id and the whole run is there.
        # uuid4() is a random id like "3f2b9c1e-...": unique without having
        # to ask a database for the next number.
        request_id = str(uuid.uuid4())
        obslog.log_event("run.started", request_id, ticket_id=ticket["ticket_id"])
        try:
            result, cost_usd = triage.classify_ticket(ticket, request_id)
        # Catch only our own typed error. Anything else is a bug in our code
        # and should crash loudly so we notice it.
        except errors.DeskhandError as exc:
            failures += 1
            # The error code goes to the table for us; the customer-safe message
            # is chosen from the taxonomy — never str(exc).
            table.add_row(
                ticket["ticket_id"],
                f"FAILED ({exc.internal_error_code})",
                "-",
                "-",
                "-",
                "-",
            )
            console.print(
                f"{ticket['ticket_id']}: {errors.user_message(exc.internal_error_code)}"
            )
            # A failed run adds nothing to total_cost: the exception carries no
            # usage, although malformed answers were still billed in tokens.
            continue
        total_cost += cost_usd
        # `result` is a TriageResult, so its fields are plain attributes:
        # no dict lookups, no KeyError if the model forgot a field.
        table.add_row(
            ticket["ticket_id"],
            result.category,
            result.priority,
            result.sentiment,
            f"{result.confidence:.2f}",
            f"${cost_usd:.4f}",
        )

    console.print(table)
    console.print(
        f"total LLM cost: ${total_cost:.4f} · failures: {failures} · log: {obslog.LOG_PATH}"
    )


# Runs main() only when started as a script (`py main.py`), not when another
# file imports it (the lab's runner does, to triage a single ticket).
if __name__ == "__main__":
    main()
