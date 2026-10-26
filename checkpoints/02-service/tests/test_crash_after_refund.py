"""DESKHAND_CRASH_AFTER_REFUND: the lab's way to die inside execute_actions,
after the refund POST and before the node's state is saved. The first run
crashes with the money moved; the re-run (variable unset) pays nothing new.
"""
from __future__ import annotations

import pytest

import nodes
# make_state: a state with a validated plan holding one $30 refund.
from test_idempotent_refund import _no_emit, make_state


class ProcessDied(BaseException):
    """Stands in for os._exit: a BaseException, so no `except Exception` in
    the runner can swallow it, just as nothing survives a real exit."""


def test_crash_lands_after_the_refund_and_the_rerun_pays_once(
    monkeypatch: pytest.MonkeyPatch, order_api
) -> None:
    def fake_exit(code: int) -> None:
        raise ProcessDied(code)

    monkeypatch.setattr(nodes.os, "_exit", fake_exit)
    monkeypatch.setenv("DESKHAND_CRASH_AFTER_REFUND", "1")
    with pytest.raises(ProcessDied):
        nodes.execute_actions(make_state(), _no_emit)
    assert order_api.refund_post_count == 1  # the money moved before the crash

    monkeypatch.delenv("DESKHAND_CRASH_AFTER_REFUND")
    assert nodes.execute_actions(make_state(), _no_emit) == "draft_customer_reply"
    assert order_api.refund_post_count == 2  # the node really ran again...
    assert len(order_api.refunds_by_key) == 1  # ...and the key kept it to one refund


def test_switch_off_never_exits(monkeypatch: pytest.MonkeyPatch, order_api) -> None:
    monkeypatch.delenv("DESKHAND_CRASH_AFTER_REFUND", raising=False)
    monkeypatch.setattr(nodes.os, "_exit", lambda code: pytest.fail("exited"))
    nodes.execute_actions(make_state(), _no_emit)
