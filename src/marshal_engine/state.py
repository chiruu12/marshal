"""Persistent fleet state — one JSON file per run.

The driver (or MCP server) can spawn a run, disconnect, and later reconnect to see status and
cost. No database: each run is its own ``runs/<run_id>.json``. One file per run means each run has
a single writer (its owning thread), so concurrent runs never contend on a shared file — the
prerequisite for parallel fan-out. Aggregates (`list`) glob the directory on read; writes are
atomic (temp file + ``os.replace``) so a concurrent reader never sees a torn file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError


class RunRecord(BaseModel):
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
    text: str = ""             # the agent's final message (file edits live in the worktree diff)
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    merged_into: str | None = None  # branch this run was integrated into, once merged
    pid: int | None = None  # OS process id of the agent subprocess, for cancel


class FleetState:
    """Per-run JSON files under a directory; one writer per run, aggregated on read."""

    def __init__(self, runs_dir: Path | str) -> None:
        self.dir = Path(runs_dir)

    def _path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.json"

    def _write(self, record: RunRecord) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(record.run_id)
        tmp = path.with_name(f"{path.name}.tmp")  # not matched by the *.json glob in list()
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a reader sees either the old file or the whole new one

    def add(self, record: RunRecord) -> None:
        self._write(record)

    def update(self, run_id: str, **fields: Any) -> RunRecord:
        rec = self.get(run_id)
        if rec is None:
            raise KeyError(run_id)
        # Re-validate the merged record (model_copy(update=...) would skip validation, so a
        # wrong-typed field could write a corrupt file that then vanishes from get()/list()).
        record = RunRecord.model_validate({**rec.model_dump(), **fields})
        self._write(record)
        return record

    def get(self, run_id: str) -> RunRecord | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[RunRecord]:
        if not self.dir.exists():
            return []
        records: list[RunRecord] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                records.append(RunRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (ValidationError, OSError):
                continue  # skip a torn/foreign file rather than failing the whole listing
        return records
