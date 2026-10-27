"""The incident board's side-by-side run: the reasoning in INCIDENT_FIXES and
the wiring in views.js. No browser, no services, no runs, no LLM.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server

STATIC = Path(__file__).resolve().parents[1] / "static"


def _cmp_row_labels() -> set[str]:
    code = (STATIC / "views.js").read_text(encoding="utf-8")
    block = re.search(r"var CMP_ROWS = \[(.*?)\n  \];", code, flags=re.S).group(1)
    return set(re.findall(r'^\s*\["([^"]+)"', block, flags=re.M))


def test_every_incident_has_its_reasoning():
    assert {inc["n"] for inc in server.INCIDENT_BOARD} == set(server.INCIDENT_FIXES)
    for n, fix in server.INCIDENT_FIXES.items():
        assert fix["broke"] and fix["broke_cites"] and fix["steps"] and fix["evidence"], n


def test_partial_incidents_say_what_is_still_open():
    for inc in server.INCIDENT_BOARD:
        assert bool(inc["retired"].get("partial")) == ("still_open" in server.INCIDENT_FIXES[inc["n"]]), inc["n"]


@pytest.mark.needs("03", "04")
def test_every_code_link_finds_its_line():
    # A checkpoint edit that moves or renames an anchor fails here, not on
    # the projector as a dashed link.
    for n, fix in server.INCIDENT_FIXES.items():
        anchors = list(fix["broke_cites"]) + [(rel, needle) for _, rel, needle in fix["steps"]]
        for rel, needle in anchors:
            c = server.cite(rel, needle)
            assert c.get("line"), f"incident #{n}: {needle!r} not found in {rel}"


def test_evidence_rows_exist_in_the_compare_table():
    labels = _cmp_row_labels()
    assert "Outcome" in labels and "Cost" in labels  # the regex found the table
    for n, fix in server.INCIDENT_FIXES.items():
        assert set(fix["evidence"]) <= labels, (n, set(fix["evidence"]) - labels)


@pytest.mark.needs("03", "04")
def test_the_board_serves_resolved_code_links():
    board = TestClient(server.app).get("/api/incidents").json()["incidents"]
    for inc in board:
        fix = inc["fix"]
        assert all(c["line"] for c in fix["broke_cites"]), inc["n"]
        assert all(s["text"] and s["cite"]["line"] for s in fix["steps"]), inc["n"]


def test_side_by_side_never_writes_markup():
    # The verdicts, the replies and plan.rationale are model text, and some
    # tickets are prompt injections: the new code goes through L.el only.
    code = (STATIC / "views.js").read_text(encoding="utf-8")
    start = code.index("function specBody(")
    end = code.index("function loadBoard(")
    assert "innerHTML" not in code[start:end]


def test_the_incidents_view_has_its_output_and_gate_policy():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="incident-out"' in html and 'id="inc-approval"' in html
    assert html.index('id="incident-out"') < html.index('id="incident-board"')
