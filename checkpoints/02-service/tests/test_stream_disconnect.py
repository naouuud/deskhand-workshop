"""A client that drops the SSE stream mid-run leaves a STREAM_DISCONNECTED line
in the log, with the last event id it saw, and the run itself is untouched.

Fully offline: the store and the SSE response class are stubbed, so the
generator behind GET /runs/{id}/events is driven directly.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app as app_module
import errors
import obslog


class FakeRequest:
    """Stands in for Starlette's Request: run_events reads only the headers
    and is_disconnected()."""

    def __init__(self, disconnected: bool, last_event_id: str | None = None) -> None:
        self.headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


@pytest.fixture()
def running_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that exists, is still running, and has no new events."""
    state = SimpleNamespace(status="running")
    monkeypatch.setattr(app_module.store, "load_state", lambda request_id: (state, "plan"))
    monkeypatch.setattr(app_module.store, "events_since", lambda request_id, seq: [])
    # Hand back the bare generator instead of wrapping it in a streaming response.
    monkeypatch.setattr(app_module, "EventSourceResponse", lambda gen: gen)


def _log_lines() -> list[dict]:
    return [json.loads(line) for line in obslog.LOG_PATH.read_text(encoding="utf-8").splitlines()]


# Path 1: the generator's own is_disconnected() check notices first.
def test_disconnect_is_logged_with_the_last_event_id(running_run) -> None:
    async def drain() -> None:
        gen = await app_module.run_events("req-1", FakeRequest(True, last_event_id="7"))
        async for _ in gen:
            pass

    asyncio.run(drain())
    [line] = [l for l in _log_lines() if l["event"] == "stream.disconnected"]
    assert line["request_id"] == "req-1"
    assert line["internal_error_code"] == errors.STREAM_DISCONNECTED
    assert line["last_event_id"] == 7


# Path 2: sse-starlette cancels the generator when the client goes away. The
# line must still be logged, and the CancelledError must still propagate so
# the server can clean up the stream.
def test_cancelled_stream_is_logged_and_still_cancelled(running_run) -> None:
    async def cancel_mid_stream() -> None:
        gen = await app_module.run_events("req-2", FakeRequest(False))
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)  # let it reach the poll sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_mid_stream())
    lines = [l for l in _log_lines() if l["event"] == "stream.disconnected"]
    assert [l["request_id"] for l in lines] == ["req-2"]
