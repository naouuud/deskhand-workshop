"""GET /api/logs, the Logs page's only server call (spec R5, R15–R18).

Fully offline: each test writes its own temp runs.jsonl files (conftest.py
points server._log_path at them) and nothing starts a service or a run.
Written before the endpoint, from specs/logs-page.md.
"""
from __future__ import annotations

import hashlib
import json

import pytest

import server
from logfiles import LOG_SOURCES, ts


def get(client, **params) -> dict:
    response = client.get("/api/logs", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def events(body: dict) -> list[str]:
    return [line["rec"].get("event") for line in body["lines"]]


# ── R5: the level is the lab's reading of the event name ─────────────────
@pytest.mark.parametrize("rec, level", [
    (None, "ERROR"),  # a line that is not JSON
    ({"event": "run.failed"}, "ERROR"),
    ({"event": "node.error", "internal_error_code": "GUARDRAIL_INJECTION_SUSPECTED"}, "ERROR"),
    ({"event": "triage.failed", "internal_error_code": "X"}, "ERROR"),  # 01's failed run
    ({"event": "guardrail.input_tripped", "internal_error_code": "GUARDRAIL_INJECTION_SUSPECTED"}, "WARN"),
    ({"event": "student.new_event", "internal_error_code": "ANY_CODE"}, "WARN"),  # any code is a warning
    ({"event": "node.started"}, "DEBUG"),
    ({"event": "node.completed"}, "DEBUG"),
    ({"event": "run.status"}, "DEBUG"),
    ({"event": "run.accepted"}, "INFO"),
    ({"event": "refund.issued"}, "INFO"),
    ({"event": "student.new_event"}, "INFO"),  # unknown events still show
])
def test_level_table(rec, level):
    assert server._log_level(rec) == level


# ── R15: the tail and the sources ────────────────────────────────────────
@pytest.mark.needs("03", "04")
def test_sources_are_the_logs_of_01_to_04(client, logs):
    logs.event("04", ts(1), "r", "run.accepted", ticket_id="T-1001")

    sources = {s["id"]: s for s in get(client)["sources"]}

    assert list(sources) == [cp for cp in LOG_SOURCES if cp in server.CHECKPOINTS]
    assert sources["04"]["label"] == "04-production"
    assert sources["04"]["path"] == "checkpoints/04-production/logs/runs.jsonl"
    assert sources["04"]["exists"] is True
    assert sources["03"]["exists"] is False  # never run: no log yet, not an error


@pytest.mark.needs("03", "04")
def test_tail_merges_all_logs_oldest_first(client, logs):
    logs.event("02", ts(2), "b", "run.accepted", ticket_id="T-1002")
    logs.event("04", ts(1), "a", "run.accepted", ticket_id="T-1001")
    logs.event("04", ts(3), "a", "node.started", node="normalize_ticket")

    body = get(client)

    assert [line["ts"] for line in body["lines"]] == [ts(1), ts(2), ts(3)]
    assert [line["src"] for line in body["lines"]] == ["04", "02", "04"]


@pytest.mark.needs("03", "04")
def test_tail_keeps_the_newest_lines(client, logs):
    for minute in range(6):
        logs.event("04", ts(minute), "r", "run.status", status="running")

    body = get(client, tail=3)

    assert [line["ts"] for line in body["lines"]] == [ts(3), ts(4), ts(5)]


@pytest.mark.needs("03", "04")
def test_a_line_is_id_src_ts_level_rec(client, logs):
    logs.event("03", ts(1), "r", "run.accepted", ticket_id="T-1001")
    offset = logs.event("03", ts(2), "r", "run.status", status="running")

    line = get(client)["lines"][1]

    assert line == {
        "id": f"03:{offset}",  # the byte offset: stable and unique
        "src": "03",
        "ts": ts(2),
        "level": "DEBUG",
        "rec": {"ts": ts(2), "request_id": "r", "event": "run.status", "status": "running"},
    }


@pytest.mark.needs("03", "04")
def test_an_unparseable_line_is_an_error_carrying_its_raw_text(client, logs):
    torn = '{"ts": "2026-10-20T10:02:00", "request_id": "r", "eve'
    logs.event("04", ts(1), "r", "run.accepted", ticket_id="T-1001")
    logs.append("04", torn)

    line = get(client)["lines"][1]

    assert line["rec"] == {"raw": torn}
    assert line["level"] == "ERROR"


def test_a_half_written_last_line_waits_for_its_newline(client, logs):
    logs.event("02", ts(1), "r", "run.accepted", ticket_id="T-1001")
    full = json.dumps({"ts": ts(2), "request_id": "r", "event": "run.status", "status": "running"})
    logs.append("02", full[:25], end="")  # the writer is mid-line

    first = get(client)
    assert events(first) == ["run.accepted"]

    with logs.path("02").open("ab") as fh:
        fh.write((full[25:] + "\n").encode("utf-8"))
    assert events(get(client, after=first["cursor"])) == ["run.status"]


# ── R16: after=<cursor>, the live tail ───────────────────────────────────
@pytest.mark.needs("03", "04")
def test_after_returns_only_what_was_appended(client, logs):
    logs.event("02", ts(1), "a", "run.accepted", ticket_id="T-1001")
    cursor = get(client)["cursor"]

    logs.event("04", ts(2), "b", "run.accepted", ticket_id="T-1002")
    logs.event("02", ts(3), "a", "run.status", status="running")
    body = get(client, after=cursor)

    assert [line["ts"] for line in body["lines"]] == [ts(2), ts(3)]
    assert get(client, after=body["cursor"])["lines"] == []


@pytest.mark.needs("03", "04")
def test_a_cleared_log_is_read_again_from_zero(client, logs):
    for minute in range(3):
        logs.event("04", ts(minute), "old", "run.status", status="running")
    cursor = get(client)["cursor"]

    logs.path("04").unlink()  # someone cleared the log
    logs.event("04", ts(10), "new", "run.accepted", ticket_id="T-1001")
    body = get(client, after=cursor)

    assert body["resets"] == ["04"]
    assert [line["rec"]["request_id"] for line in body["lines"]] == ["new"]


# ── R17: request_id=<id>, a whole run ────────────────────────────────────
@pytest.mark.needs("03", "04")
def test_request_id_finds_every_line_of_that_run_and_nothing_else(client, logs):
    logs.event("04", ts(1), "abc", "run.accepted", ticket_id="T-1001")
    logs.event("04", ts(2), "abcd", "run.accepted", ticket_id="T-1002")  # a longer id
    logs.event("02", ts(3), "abc", "run.status", status="running")
    logs.event("04", ts(4), "abc", "run.completed", reply="Done.")

    body = get(client, request_id="abc")

    assert [line["ts"] for line in body["lines"]] == [ts(1), ts(3), ts(4)]


# ── R18: read only, redacted ─────────────────────────────────────────────
@pytest.mark.needs("03", "04")
def test_secrets_are_redacted(client, logs, monkeypatch):
    secret = "sk-test-0123456789abcdef"
    monkeypatch.setattr(server, "_secrets", lambda: [secret])
    logs.event("04", ts(1), "abc", "run.completed", reply=f"your key is {secret}")

    text = json.dumps(get(client))

    assert secret not in text
    assert "[redacted]" in text


def test_reading_never_changes_a_log(client, logs):
    logs.event("04", ts(1), "abc", "run.accepted", ticket_id="T-1001")
    before = hashlib.sha256(logs.path("04").read_bytes()).hexdigest()

    get(client, after=get(client)["cursor"])
    get(client, request_id="abc")

    assert hashlib.sha256(logs.path("04").read_bytes()).hexdigest() == before
