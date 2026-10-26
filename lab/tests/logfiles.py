"""Temp runs.jsonl files for the Logs tests, written the way obslog writes them."""
from __future__ import annotations

import json
from pathlib import Path

# The checkpoints that write a runs.jsonl (00 has no log: that is its lesson).
LOG_SOURCES = ["01", "02", "03", "04"]


class LogFiles:
    """One temp runs.jsonl per checkpoint, under one root folder."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, cp: str) -> Path:
        return self.root / cp / "logs" / "runs.jsonl"

    def append(self, cp: str, line: dict | str, end: str = "\n") -> int:
        """Append one line and return the byte offset it starts at. A dict is
        written as obslog would (json.dumps, ensure_ascii=False); a str is
        written as-is, for torn and malformed lines."""
        text = line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)
        path = self.path(cp)
        path.parent.mkdir(parents=True, exist_ok=True)
        offset = path.stat().st_size if path.exists() else 0
        with path.open("ab") as fh:
            fh.write((text + end).encode("utf-8"))
        return offset

    def event(self, cp: str, ts: str, rid: str, event: str, **fields) -> int:
        return self.append(cp, {"ts": ts, "request_id": rid, "event": event, **fields})


def ts(minute: int, second: int = 0) -> str:
    """A UTC timestamp in obslog's format, on a fixed day, so order is easy to read."""
    return f"2026-10-20T10:{minute:02d}:{second:02d}.000000+00:00"
