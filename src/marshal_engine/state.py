"""Persistent fleet state — a JSON record of every run.

The driver (or MCP server) can spawn a run, disconnect, and later reconnect to see status and
cost. No database: a single `fleet.json` keyed by run id.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class RunRecord:
    run_id: str
    task_id: str
    backend: str
    status: str = "queued"  # queued|running|succeeded|failed|timed_out|cancelled
    client: str | None = None
    model: str | None = None
    worktree: str | None = None
    branch: str | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    source: str | None = None  # cost provenance: native | estimated | unavailable | ...
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    merged_into: str | None = None  # branch this run was integrated into, once merged


class FleetState:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"runs": {}}
        data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("runs", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add(self, record: RunRecord) -> None:
        data = self._load()
        data["runs"][record.run_id] = asdict(record)
        self._save(data)

    def update(self, run_id: str, **fields: Any) -> RunRecord:
        data = self._load()
        if run_id not in data["runs"]:
            raise KeyError(run_id)
        data["runs"][run_id].update(fields)
        self._save(data)
        return RunRecord(**data["runs"][run_id])

    def get(self, run_id: str) -> RunRecord | None:
        raw = self._load()["runs"].get(run_id)
        return RunRecord(**raw) if raw else None

    def list(self) -> list[RunRecord]:
        return [RunRecord(**r) for r in self._load()["runs"].values()]
