"""Shared offline fixtures for the lab's tests. No services, no runs, no LLM.

The Logs page reads the four checkpoints' logs/runs.jsonl. These fixtures
point server._log_path at empty files under tmp_path instead, so a test
writes exactly the lines it needs and the real logs are never read or
touched. Spec: specs/logs-page.md.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from logfiles import LogFiles


@pytest.fixture
def logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LogFiles:
    files = LogFiles(tmp_path)
    # The endpoint must resolve paths through _log_path at request time (R18).
    monkeypatch.setattr(server, "_log_path", files.path)
    # redact() reads the real .env; no secrets by default keeps tests deterministic.
    monkeypatch.setattr(server, "_secrets", lambda: [])
    return files


@pytest.fixture
def client(logs: LogFiles) -> TestClient:
    # No `with` block: nothing at startup is wanted here.
    return TestClient(server.app)
