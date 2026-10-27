"""GET /api/state/{cp} and GET /api/state/{cp}/runs/{id}, the Saved state
page's endpoints. Offline: every database is a temp file built with the
checkpoint's own schema (read from store.py, never copied), and the real
runs.db files are never opened. One test runs the real cpstate.py runner, so
the "a restart picks it up" answer is the checkpoint's store.find_resumable().
"""
from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server

STORE_PY = Path(server.REPO) / "checkpoints" / "02-service" / "store.py"


def _store_schema() -> str:
    """store.py's _SCHEMA string, read from its source."""
    for node in ast.parse(STORE_PY.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "_SCHEMA":
            return ast.literal_eval(node.value)
    raise AssertionError("store.py has no _SCHEMA")


def _state(request_id: str, status: str, **extra) -> str:
    return json.dumps({"request_id": request_id, "ticket_id": extra.pop("ticket_id", "T-1001"),
                       "ticket_text": "My headphones arrived cracked.", "status": status, **extra})


class StateFiles:
    """A tmp folder per checkpoint that _state_path points into."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, checkpoint: str, name: str) -> Path:
        folder = self.root / checkpoint
        folder.mkdir(exist_ok=True)
        return folder / name

    def runs_db(self, checkpoint: str = "02") -> sqlite3.Connection:
        conn = sqlite3.connect(self.path(checkpoint, "runs.db"))
        conn.execute("PRAGMA journal_mode=WAL")  # as store._connect leaves it
        conn.executescript(_store_schema())
        return conn

    def add_run(self, conn: sqlite3.Connection, request_id: str, status: str, next_node: str | None,
                updated_at: str, events: list[tuple[str, dict]] = (), **extra) -> None:
        conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                     (request_id, status, next_node, _state(request_id, status, **extra), updated_at))
        for seq, (event, data) in enumerate(events, start=1):
            conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                         (request_id, seq, event, json.dumps(data), updated_at))
        conn.commit()


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StateFiles:
    f = StateFiles(tmp_path)
    monkeypatch.setattr(server, "_state_path", f.path)
    monkeypatch.setattr(server, "_secrets", lambda: [])
    monkeypatch.setattr(server, "_resumable_cache", {})
    return f


@pytest.fixture
def lab() -> TestClient:
    return TestClient(server.app)


def _fake_runner(monkeypatch: pytest.MonkeyPatch, runs: list[dict] | None = None, error: str | None = None) -> list[dict]:
    """Replace the cpstate.py runner; returns the list of requests it got."""
    calls: list[dict] = []

    def spawn(checkpoint, request, stop=None, runner=None):
        calls.append({"checkpoint": checkpoint, "runner": runner, **request})
        if error:
            yield {"type": "error", "message": "resumable failed", "detail": error}
        else:
            yield {"type": "resumable", "runs": runs or []}

    monkeypatch.setattr(server, "_spawn", spawn)
    return calls


def test_without_runs_db_the_page_says_so_and_creates_nothing(files, lab, monkeypatch):
    calls = _fake_runner(monkeypatch)
    body = lab.get("/api/state/02").json()
    runs_db = [f for f in body["files"] if f["name"] == "runs.db"][0]
    assert runs_db["exists"] is False and body["rows"] == [] and body["total"] == 0
    assert calls == [], "store's connection would create runs.db: the runner must not be asked"
    assert not files.path("02", "runs.db").exists()


def test_rows_come_newest_first_with_their_summary(files, lab, monkeypatch):
    _fake_runner(monkeypatch)
    conn = files.runs_db()
    files.add_run(conn, "aaaa1111", "completed", None, "2026-10-24T10:00:00+00:00",
                  events=[("node.started", {"node": "triage"}), ("node.completed", {"node": "triage"})],
                  customer_name="Dana Whitfield", cost_usd=0.0012)
    files.add_run(conn, "bbbb2222", "awaiting_approval", None, "2026-10-24T11:00:00+00:00",
                  approval_status="pending")
    files.add_run(conn, "cccc3333", "failed", None, "2026-10-24T09:00:00+00:00",
                  ticket_id="adhoc-cccc3333", internal_error_code="TOOL_ORDER_API_ERROR")
    body = lab.get("/api/state/02").json()
    assert [r["request_id"] for r in body["rows"]] == ["bbbb2222", "aaaa1111", "cccc3333"]
    assert body["total"] == 3
    assert body["counts"] == {"completed": 1, "awaiting_approval": 1, "failed": 1}
    first = {r["request_id"]: r for r in body["rows"]}
    assert first["aaaa1111"]["events"] == 2 and first["aaaa1111"]["customer_name"] == "Dana Whitfield"
    assert first["cccc3333"]["internal_error_code"] == "TOOL_ORDER_API_ERROR"
    assert first["bbbb2222"]["steps"] is None  # no runs.langgraph.db
    tables = {t["name"]: t["rows"] for t in body["files"][0]["tables"]}
    assert tables == {"events": 2, "runs": 3}


def test_the_rows_are_capped_but_the_counts_are_not(files, lab, monkeypatch):
    _fake_runner(monkeypatch)
    monkeypatch.setattr(server, "STATE_RUNS_MAX", 2)
    conn = files.runs_db()
    for i in range(5):
        files.add_run(conn, f"run{i:05d}", "completed", None, f"2026-10-24T1{i}:00:00+00:00")
    body = lab.get("/api/state/02").json()
    assert body["total"] == 5 and len(body["rows"]) == 2
    assert body["rows"][0]["request_id"] == "run00004"


def test_a_secret_in_the_saved_state_is_redacted(files, lab, monkeypatch):
    _fake_runner(monkeypatch)
    monkeypatch.setattr(server, "_secrets", lambda: ["sk-supersecret"])
    conn = files.runs_db()
    files.add_run(conn, "aaaa1111", "completed", None, "2026-10-24T10:00:00+00:00",
                  customer_name="sk-supersecret", events=[("run.completed", {"reply": "key sk-supersecret"})])
    assert "sk-supersecret" not in lab.get("/api/state/02").text
    detail = lab.get("/api/state/02/runs/aaaa1111").text
    assert "sk-supersecret" not in detail and "[redacted]" in detail


def test_which_runs_a_restart_picks_up_is_the_checkpoints_own_answer(files, lab):
    # The real runner, the real checkpoint 02 store.find_resumable(), a temp runs.db.
    conn = files.runs_db()
    files.add_run(conn, "run-running", "running", "fetch_order_data", "2026-10-24T10:00:00+00:00")
    files.add_run(conn, "run-queued", "queued", "normalize_ticket", "2026-10-24T10:01:00+00:00")
    files.add_run(conn, "run-waiting", "awaiting_approval", None, "2026-10-24T10:02:00+00:00")
    files.add_run(conn, "run-done", "completed", None, "2026-10-24T10:03:00+00:00")
    files.add_run(conn, "run-failed", "failed", None, "2026-10-24T10:04:00+00:00")
    res = lab.get("/api/state/02").json()["resumable"]
    assert res["ok"] is True, res
    assert sorted((r["request_id"], r["next_node"]) for r in res["runs"]) == [
        ("run-queued", "normalize_ticket"), ("run-running", "fetch_order_data")]


def test_the_answer_is_kept_until_runs_db_changes(files, lab, monkeypatch):
    calls = _fake_runner(monkeypatch, runs=[{"request_id": "aaaa1111", "status": "running", "next_node": "triage"}])
    conn = files.runs_db()
    files.add_run(conn, "aaaa1111", "running", "triage", "2026-10-24T10:00:00+00:00")
    lab.get("/api/state/02")
    lab.get("/api/state/02")
    assert len(calls) == 1
    assert calls[0]["runner"] == "cpstate.py" and calls[0]["mode"] == "resumable"
    assert calls[0]["db_path"] == str(files.path("02", "runs.db"))
    files.add_run(conn, "bbbb2222", "completed", None, "2026-10-24T10:05:00+00:00")
    body = lab.get("/api/state/02").json()
    assert len(calls) == 2
    assert body["resumable"]["runs"][0]["request_id"] == "aaaa1111"


def test_a_runner_that_fails_leaves_the_rows_and_says_why(files, lab, monkeypatch):
    _fake_runner(monkeypatch, error="ValidationError: status")
    conn = files.runs_db()
    files.add_run(conn, "aaaa1111", "running", "triage", "2026-10-24T10:00:00+00:00")
    body = lab.get("/api/state/02").json()
    assert body["rows"] and body["resumable"]["ok"] is False
    assert "ValidationError" in body["resumable"]["error"]


def test_one_run_has_its_state_and_its_events_in_order(files, lab, monkeypatch):
    _fake_runner(monkeypatch)
    conn = files.runs_db()
    files.add_run(conn, "aaaa1111", "running", "generate_resolution_plan", "2026-10-24T10:00:00+00:00",
                  events=[("node.started", {"node": "triage"}), ("node.completed", {"node": "triage", "latency_ms": 900}),
                          ("node.started", {"node": "fetch_order_data"})])
    d = lab.get("/api/state/02/runs/aaaa1111").json()
    assert d["status"] == "running" and d["next_node"] == "generate_resolution_plan"
    assert d["state"]["ticket_text"] == "My headphones arrived cracked."
    assert [e["seq"] for e in d["events"]] == [1, 2, 3] and d["events_total"] == 3
    assert d["events"][1]["data"] == {"node": "triage", "latency_ms": 900}
    assert d["langgraph"] is None


@pytest.mark.parametrize("url, code", [
    ("/api/state/01", 422),                    # 01 saves nothing: not a service
    ("/api/state/99", 404),
    ("/api/state/02/runs/zz", 422),            # not a request id
    ("/api/state/02/runs/nosuchrun", 404),
])
def test_bad_requests(files, lab, monkeypatch, url, code):
    _fake_runner(monkeypatch)
    files.runs_db()
    assert lab.get(url).status_code == code


def _langgraph_db(files: StateFiles, checkpoint: str = "03") -> sqlite3.Connection:
    """The two SqliteSaver tables, with the columns the endpoint reads."""
    conn = sqlite3.connect(files.path(checkpoint, "runs.langgraph.db"))
    conn.executescript("""
        CREATE TABLE checkpoints (thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id));
        CREATE TABLE writes (thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL, channel TEXT NOT NULL,
            type TEXT, value BLOB, PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx));
    """)
    return conn


@pytest.mark.needs("03", "04")
def test_langgraph_steps_read_the_path_off_the_writes(files, lab, monkeypatch):
    _fake_runner(monkeypatch)
    conn = files.runs_db("03")
    files.add_run(conn, "aaaa1111", "completed", None, "2026-10-24T10:00:00+00:00")
    lg = _langgraph_db(files)
    steps = [  # (checkpoint id, metadata, channels written from it)
        ("c0", {"source": "input", "step": -1}, ["request_id", "branch:to:normalize_ticket"]),
        ("c1", {"source": "loop", "step": 0}, ["status", "branch:to:approval_gate"]),
        ("c2", {"source": "loop", "step": 1}, ["__interrupt__", "__resume__", "branch:to:finalize"]),
        ("c3", {"source": "loop", "step": 2}, []),
    ]
    for cid, meta, channels in steps:
        lg.execute("INSERT INTO checkpoints VALUES ('aaaa1111', '', ?, NULL, 'msgpack', x'00', ?)",
                   (cid, json.dumps(meta).encode()))
        for i, ch in enumerate(channels):
            lg.execute("INSERT INTO writes VALUES ('aaaa1111', '', ?, 'task', ?, ?, 'msgpack', x'00')", (cid, i, ch))
    lg.commit()
    assert lab.get("/api/state/03").json()["rows"][0]["steps"] == 4
    got = lab.get("/api/state/03/runs/aaaa1111").json()["langgraph"]["steps"]
    assert [(s["step"], s["ran"], s["next"]) for s in got] == [
        (-1, None, ["normalize_ticket"]), (0, "normalize_ticket", ["approval_gate"]),
        (1, "approval_gate", ["finalize"]), (2, "finalize", [])]
    assert got[2]["special"] == ["__interrupt__", "__resume__"]


def test_reading_never_writes_the_database(files, lab, monkeypatch):
    _fake_runner(monkeypatch)
    conn = files.runs_db()
    files.add_run(conn, "aaaa1111", "completed", None, "2026-10-24T10:00:00+00:00")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    path = files.path("02", "runs.db")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    lab.get("/api/state/02")
    lab.get("/api/state/02/runs/aaaa1111")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    with pytest.raises(sqlite3.OperationalError):
        server._read_only(path).execute("DELETE FROM runs")
