"""JSONL trace：一次运行一个文件，每行一个事件，带 UTC 时间戳与 trace_id。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TraceWriter:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.trace_id = uuid.uuid4().hex[:12]
        self._fh = self.path.open("a", encoding="utf-8")

    def emit(self, *, task_id: str, event: str, **fields) -> dict:
        rec = {
            "ts": _utc(),
            "trace_id": self.trace_id,
            "task_id": task_id,
            "event": event,
            **fields,
        }
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        return rec

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def read_events(path: Path) -> list[dict]:
    events = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
