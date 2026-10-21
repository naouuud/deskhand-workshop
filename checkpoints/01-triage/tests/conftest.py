"""Shared offline test fixtures. No live LLM calls, no live HTTP.

pytest loads a file named conftest.py automatically before running the tests
next to it. Fixtures defined here are available to every test file in this
folder without an import.
"""

import sys
from pathlib import Path

import pytest

# Make the checkpoint's modules importable when pytest runs from anywhere.
# (01 has no root conftest.py, so it is done here, before the import below.)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import obslog  # noqa: E402

# Each test file replaces the provider call itself (monkeypatch), so no test
# needs a key or spends money. This file only redirects the log.


# A fixture is setup code pytest runs for a test. autouse: every test gets
# this without asking for it, so none can forget.
@pytest.fixture(autouse=True)
def isolate_logs(monkeypatch, tmp_path):
    """Keep test runs from appending to the real logs/runs.jsonl."""
    # monkeypatch swaps an attribute for the length of one test and puts the
    # original back afterwards. tmp_path is a fresh empty folder per test.
    monkeypatch.setattr(obslog, "LOG_PATH", tmp_path / "runs.jsonl")
