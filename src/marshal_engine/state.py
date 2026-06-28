"""Persistent fleet state - one JSON file per run.

The driver (or MCP server) can spawn a run, disconnect, and later reconnect to see status and
cost. No database: each run is its own ``runs/<run_id>.json``. Most runs have a single writer (the
thread executing them), but ``cancel_run`` legitimately writes the *same* run concurrently from
another thread - so per-run writes are serialized by a per-run lock, and each write goes through a
*unique* temp file before an atomic ``os.replace`` (a fixed temp name would let two concurrent
writers clobber each other's temp). Aggregates (``list``) glob the directory on read and skip a
torn/foreign file rather than failing the whole listing.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from collections.abc import Callable
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
    attempts: int = 1  # how many times the backend was run (>1 means a transient failure was retried)


class FleetState:
    """Per-run JSON files under a directory; per-run-locked writes, aggregated on read."""

    def __init__(self, runs_dir: Path | str) -> None:
        self.dir = Path(runs_dir)
        # One lock per run_id serializes the read-modify-write in update()/update_if() so a run's
        # terminal stamp and a concurrent cancel can't lose each other's update.
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.json"

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(run_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[run_id] = lock
            return lock

    def _write(self, record: RunRecord) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(record.run_id)
        # A UNIQUE temp in the same dir (not a fixed "<id>.json.tmp"), so two concurrent writers to
        # the same run can't remove each other's temp and crash the os.replace.
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=f"{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(record.model_dump_json(indent=2))
            os.replace(tmp, path)  # atomic: a reader sees either the old file or the whole new one
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def add(self, record: RunRecord) -> None:
        self._write(record)

    def update(self, run_id: str, **fields: Any) -> RunRecord:
        """Merge fields into a run and persist it. Serialized per run_id."""
        with self._lock_for(run_id):
            return self._update_locked(run_id, fields)

    def update_if(
        self, run_id: str, predicate: Callable[[RunRecord], bool], **fields: Any
    ) -> RunRecord:
        """Merge fields only if ``predicate(current)`` holds; return the (possibly unchanged) record.

        Read-check-write under the per-run lock, so a conditional transition (e.g. cancel: "set
        cancelled only if still running") can't be raced by a concurrent terminal stamp.
        """
        with self._lock_for(run_id):
            rec = self.get(run_id)
            if rec is None:
                raise KeyError(run_id)
            if not predicate(rec):
                return rec
            return self._update_locked(run_id, fields)

    def _update_locked(self, run_id: str, fields: dict[str, Any]) -> RunRecord:
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
            except (ValidationError, OSError, ValueError):
                continue  # skip a torn/foreign/binary file (ValueError covers UnicodeDecodeError)
        return records
