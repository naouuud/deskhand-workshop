"""The surfaces the browser console depends on. Fully offline — no LLM, no
order API, no runs started.

TestClient is built without a `with` block on purpose: that skips the lifespan,
so these tests never touch runs.db or try to resume stuck runs.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


# The picker's contract: every ticket in the dataset, with exactly these
# fields, and a preview capped at 160 characters.
def test_ticket_catalogue_covers_the_shared_dataset():
    body = client.get("/tickets").json()
    tickets = body["tickets"]

    assert len(tickets) == 60
    assert {t["ticket_id"] for t in tickets} >= {"T-1001", "T-1034"}

    for ticket in tickets:
        assert ticket.keys() == {
            "ticket_id",
            "customer_name",
            "order_id",
            "channel",
            "tags",
            "message_count",
            "preview",
        }
        assert ticket["preview"], f"{ticket['ticket_id']} has no preview text"
        assert len(ticket["preview"]) <= 160


def test_catalogue_never_leaks_full_ticket_text():
    """The picker gets a preview; the full thread stays server-side and is only
    assembled inside POST /tickets."""
    long_thread = next(
        t for t in client.get("/tickets").json()["tickets"] if t["message_count"] > 1
    )
    assert "[customer]" not in long_thread["preview"]


# An empty body is a 422 before any run (or model call) exists.
def test_submit_still_requires_one_of_the_two_fields():
    assert client.post("/tickets", json={}).status_code == 422


# The service, not a separate dev server, serves the console, and / lands
# on it. A missing file here means a blank page in class.
def test_console_is_served_and_root_redirects():
    assert app_module.CONSOLE_DIR.is_dir()

    page = client.get("/console/")
    assert page.status_code == 200
    assert "Deskhand Console" in page.text

    for asset in ("console.css", "console.js"):
        assert client.get(f"/console/{asset}").status_code == 200

    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/console/"


def test_console_js_tracks_the_real_node_and_event_names():
    """The pipeline strip and the SSE listeners are hand-written copies of the
    server's contract. If a node or event is renamed, fail here rather than
    silently rendering a dead pill."""
    import events
    import nodes

    source = (app_module.CONSOLE_DIR / "console.js").read_text(encoding="utf-8")

    for node_name in nodes.NODES:
        assert f'"{node_name}"' in source, f"console.js is missing node {node_name}"

    published = {
        value
        for name, value in vars(events).items()
        if name.isupper() and isinstance(value, str) and "." in value
    }
    for event_name in published:
        assert f'"{event_name}"' in source, f"console.js is missing event {event_name}"
