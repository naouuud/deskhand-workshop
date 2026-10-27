"""GET and POST /api/clean-slate, the top bar's Clean slate button, and its
wiring in index.html. Offline: logs and databases are temp files (the
fixtures from conftest.py and test_state_api.py), the mock is never asked,
and no service is started or stopped.
"""
from __future__ import annotations

import re
import sqlite3
from collections import OrderedDict, deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from test_logs_ui import _Page
from test_state_api import StateFiles

STATIC = Path(__file__).resolve().parents[1] / "static"


@pytest.fixture
def files(tmp_path: Path, logs, monkeypatch: pytest.MonkeyPatch) -> StateFiles:
    f = StateFiles(tmp_path / "state")
    f.root.mkdir()
    monkeypatch.setattr(server, "_state_path", f.path)
    monkeypatch.setattr(server, "_resumable_cache", {"02": ((), {"ok": True, "runs": []})})
    monkeypatch.setattr(server, "_probe_sync", lambda *a, **k: None)  # the mock is down
    monkeypatch.setattr(server, "MANAGED", {})
    monkeypatch.setattr(server, "LIVE", {})
    monkeypatch.setattr(server, "RUNS", OrderedDict(a={"run_id": "a"}))
    monkeypatch.setattr(server, "RECENT", deque([{"request_id": "r1"}], maxlen=25))
    monkeypatch.setattr(server, "OBSERVED_COST", {("01", "*"): 0.01})
    return f


@pytest.fixture
def lab(files) -> TestClient:
    return TestClient(server.app)


def _seed(logs, files: StateFiles) -> None:
    logs.event("01", "2026-10-20T10:00:00+00:00", "r1", "run.started")
    logs.event("04", "2026-10-20T10:00:01+00:00", "r2", "run.started")
    logs.event("04", "2026-10-20T10:00:02+00:00", "r2", "run.completed")
    conn = files.runs_db("02")
    files.add_run(conn, "r1", "COMPLETED", None, "2026-10-20T10:00:00", events=[("node", {"n": 1})])
    conn.close()


@pytest.mark.needs("03", "04")
def test_preview_says_what_there_is_and_deletes_nothing(lab, logs, files):
    _seed(logs, files)
    body = lab.get("/api/clean-slate").json()
    by_cp = {f["checkpoint"]: f for f in body["logs"]}
    assert by_cp["01"]["lines"] == 1 and by_cp["04"]["lines"] == 2
    assert not by_cp["02"]["exists"]
    assert [(s["checkpoint"], s["rows"]) for s in body["state"]] == [("02", 2)]  # one run, one event
    assert body["runs"] == {"traces": 1, "recent": 1}
    assert body["till"]["up"] is False and body["open_runs"] == 0
    assert logs.path("01").exists() and files.path("02", "runs.db").exists()


@pytest.mark.needs("03", "04")
def test_logs_deletes_every_runs_jsonl_and_nothing_else(lab, logs, files):
    _seed(logs, files)
    done = lab.post("/api/clean-slate", json={"logs": True}).json()["done"]
    assert all(d["ok"] for d in done)
    assert not logs.path("01").exists() and not logs.path("04").exists()
    with sqlite3.connect(files.path("02", "runs.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert len(server.RUNS) == 1


def test_state_empties_the_rows_and_keeps_the_tables(lab, logs, files):
    _seed(logs, files)
    done = lab.post("/api/clean-slate", json={"state": True}).json()["done"]
    assert done == [{"what": "checkpoints/02-service/runs.db", "ok": True, "note": "deleted 2 rows from 2 tables"}]
    with sqlite3.connect(files.path("02", "runs.db")) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"runs", "events"} <= tables  # store.py keeps saving without a restart
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert server._resumable_cache == {}
    assert not files.path("03", "runs.db").exists()  # never creates a file
    assert logs.path("01").exists()


def test_runs_forgets_the_lab_servers_memory(lab, files):
    done = lab.post("/api/clean-slate", json={"runs": True}).json()["done"]
    assert done[0]["ok"] and "1 trace" in done[0]["note"]
    assert not server.RUNS and not server.RECENT and not server.OBSERVED_COST


def test_till_with_the_mock_down_has_nothing_to_restart(lab, files):
    done = lab.post("/api/clean-slate", json={"till": True}).json()["done"]
    assert done == [{"what": "mock order API refunds", "ok": True, "note": "not running: it starts with an empty till"}]


def test_till_restarts_a_mock_the_lab_started_with_the_same_flags(lab, files, monkeypatch):
    calls = []

    class Proc:
        def poll(self):
            return None

    server.MANAGED["mock"] = {"proc": Proc(), "flag_env": {"CHAOS_MODE": "slow"}}
    monkeypatch.setattr(server, "stop_service", lambda name: calls.append(("stop", name)))
    monkeypatch.setattr(server, "start_service", lambda name, flags=None: calls.append(("start", name, flags)))
    done = lab.post("/api/clean-slate", json={"till": True}).json()["done"]
    assert calls == [("stop", "mock"), ("start", "mock", {"CHAOS_MODE": "slow"})]
    assert done[0]["ok"] and "same flags" in done[0]["note"]


def test_till_never_restarts_a_mock_started_in_a_terminal(lab, files, monkeypatch):
    monkeypatch.setattr(server, "_probe_sync", lambda url, **k: {"count": 2} if url.endswith("/refunds") else {})
    monkeypatch.setattr(server, "stop_service", lambda name: pytest.fail("stopped a mock the lab did not start"))
    done = lab.post("/api/clean-slate", json={"till": True}).json()["done"]
    assert done[0]["ok"] is False and "terminal" in done[0]["note"]


def test_refused_while_a_run_is_open(lab, logs, files):
    _seed(logs, files)
    server.LIVE["x"] = {"checkpoint": "02"}
    r = lab.post("/api/clean-slate", json={"logs": True, "state": True, "runs": True})
    assert r.status_code == 409
    assert logs.path("01").exists() and len(server.RUNS) == 1


def test_unknown_parts_are_rejected(lab):
    assert lab.post("/api/clean-slate", json={"everything": True}).status_code == 422


# ---------------------------------------------------------------- the page


def test_the_button_is_in_the_top_bar_so_every_view_has_it():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    topbar = html[html.index('<header class="topbar">'):html.index("</header>")]
    assert 'id="clean-slate-btn"' in topbar


def test_cleanslate_js_loads_after_core_and_before_views():
    page = _Page()
    page.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    s = page.scripts
    assert s.index("core.js") < s.index("cleanslate.js") < s.index("views.js")


def test_cleanslate_js_asks_first_and_never_uses_innerhtml():
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", (STATIC / "cleanslate.js").read_text(encoding="utf-8"), flags=re.S)
    assert "innerHTML" not in code
    # The POST happens only inside the dialog's Delete click, after the GET.
    assert code.index('L.getJSON("/api/clean-slate")') < code.index("yes.onclick") < code.index('L.postJSON("/api/clean-slate"')
