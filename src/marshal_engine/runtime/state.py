"""Persistent fleet state - one JSON file per run.

The driver (or MCP server) can spawn a run, disconnect, and later reconnect to see status and
cost. No database: each run is its own ``runs/<run_id>.json``. Most runs have a single writer (the
thread executing them), but ``cancel_run`` legitimately writes the *same* run concurrently from
another thread - and a CLI + long-lived MCP server can race the same record across processes - so
per-run writes are serialized by a per-run ``threading.Lock`` (in-process) plus an ``fcntl.flock``
on a ``runs/<run_id>.json.lock`` sidecar (cross-process). Each write goes through a *unique* temp
file before an atomic ``os.replace`` (a fixed temp name would let two concurrent writers clobber
each other's temp). Aggregates (``list``) glob ``*.json`` on read - so ``.json.lock`` sidecars are
never mistaken for records - and skip a torn/foreign file rather than failing the whole listing.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator

from ..core.types import canonical_status
from .worktree import is_git_object_id, validate_run_id


class RunRecord(BaseModel):
    run_id: str
    task_id: str
    backend: str
    status: str = "queued"  # queued|running|succeeded|empty|failed|timed_out|cancelled|verify_failed
    client: str | None = None
    model: str | None = None
    worktree: str | None = None
    branch: str | None = None
    base_branch: str | None = None  # branch the worktree was cut from (None = pre-drift records)
    # The COMMIT that branch pointed at when the run was spawned. A branch name is mutable: if
    # `main` moves while the agent works, reviewing against the name compares to a different base
    # than the agent started from. The sha is what the run actually branched off.
    base_commit: str | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    source: str | None = None  # cost provenance: native | admin-api | unavailable | ...
    text: str = ""             # the agent's final message (file edits live in the worktree diff)
    # Schema-validated JSON object when the task requested output_schema and the final message
    # conformed. Absent/None on records written before this field existed, and on runs that did
    # not request a schema (or failed validation - see error).
    structured: dict[str, Any] | None = None
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    merged_into: str | None = None  # branch this run was integrated into, once merged
    # Judgment about the work, distinct from process ``status``. Values: ``integrated`` /
    # ``rejected`` / ``abandoned``. Late-arriving (integrate succeeds after the usage event was
    # written): stamped here rather than rewriting ``events.jsonl`` or appending a second cost
    # event that would double-count in the rollup. Absence means "no judgment yet", never rejected.
    outcome: str | None = None
    # When the outcome was recorded (UTC isoformat), and the reviewer's short reason. Both additive
    # and default None, so records written before they existed still parse. The note is TRUNCATED
    # on write - `text` and `verify_output` already taught us what an unbounded field costs every
    # reader of a record (see _BULKY_FIELDS).
    outcome_at: str | None = None
    outcome_note: str | None = None
    commit: str | None = None  # branch tip after commit_run froze the work (for chaining/integrate)
    pid: int | None = None  # OS process id of the agent subprocess, for cancel
    # Start time of `pid` as the OS reports it. Pids are reused, so this is what makes the pid an
    # identity: a live pid whose start time differs is somebody else's process, never our agent.
    pid_start_time: str | None = None
    attempts: int = 1  # how many times the backend was run (>1 means a transient failure was retried)
    # Outcome of the workspace's optional `verify:` gate. None = no gate ran (unconfigured, run
    # not would-be-succeeded, or no file changes to gate); False pairs with status `verify_failed`.
    verify_passed: bool | None = None
    verify_output: str = ""  # tail-truncated verify command output (failures print last)
    # The command that provisioned this run's worktree, or None when none was configured (the
    # worktree is then a bare checkout: no venv, no extras, no gitignored data dirs).
    #
    # Recorded because a NUMBER from a worktree does not mean the same thing as the same number
    # from your checkout, and nothing marked the difference. Reported in the field: agents said
    # "1308 passed" where the workspace showed "1351 passed, 0 skipped" - a bare `uv sync` had left
    # the extras uninstalled - and it took three occurrences before a driver with full context
    # recognised the pattern. Marshal does not own `worktree_setup` (it is the user's config), but
    # it does own whether the difference is visible on the result.
    worktree_setup: str | None = None
    # Declared outside-worktree paths this run was allowed to read (copied under `.marshal-context/`).
    # Empty means the run saw only its worktree. Surfaced so a reviewer can tell when a run saw more.
    read_paths: list[str] = []
    # Files this run wrote to `.marshal-artifacts/`, harvested to `.marshal/artifacts/<run_id>/`
    # and therefore still readable after the worktree is gone. Names only - the content stays on
    # disk. A later run reads them by naming this run in its `artifacts_from`.
    artifacts: list[str] = []
    # DERIVED ON READ, never persisted (see FleetState._write). Whether the agent process is alive
    # right now: True/False when the pid's identity could be checked, None when it could not (no
    # pid recorded, or the probe is unavailable) - and None on any terminal record, where the
    # question is meaningless. A driver reading `running` cannot otherwise tell "still working"
    # from "finished, outcome not yet written", and guessing wrong misreports a run's outcome.
    # Persisting it would recreate exactly that bug: a stored liveness is stale the moment it lands.
    agent_alive: bool | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _migrate_status(cls, value: Any) -> Any:
        """Accept the pre-rename spelling on read; write the new one.

        `succeeded` overclaimed - it means the process exited 0, not that the work is right. Ledgers
        written before the rename are not rewritten: they load as `exited_clean` and are written
        back that way the next time the record is updated.
        """
        return canonical_status(value) if isinstance(value, str) else value

    @field_validator("base_commit", mode="before")
    @classmethod
    def _require_sha_shaped_base_commit(cls, value: Any) -> Any:
        """Keep ``base_commit`` sha-shaped; strip poison rather than reject the record.

        A failed ``git rev-parse`` used to echo the ref name into this field. Rejecting the whole
        record would break listing old ledgers; coercing to None matches the then-chain rule that
        a missing base is not evidence of no diff.
        """
        if value is None or value == "":
            return None
        if isinstance(value, str) and is_git_object_id(value):
            return value
        return None


#: Fields computed when a record is READ and deliberately kept out of the ledger. The ledger holds
#: facts about what happened; these are answers about right now, and storing one would guarantee it
#: is wrong later.
_DERIVED_FIELDS = {"agent_alive"}


#: Unbounded free text on a run record. A listing carries one row per run, so these dominate its
#: size - one observed `status` reply was ~395k characters, most of it agent prose the caller had
#: not asked for. Omitted from a listing by default and fetched per run with `get_run`.
_BULKY_FIELDS = ("text", "verify_output")

#: Test-only seam: called between read and write inside the locked RMW in ``_update_locked``.
#: Production leaves this ``None``. Tests may assign a short sleep to widen the race window when
#: verifying that the flock prevents sibling-field loss under concurrent writers.
_rmw_between_read_write: Callable[[], None] | None = None


def compact_run(rec: RunRecord) -> dict[str, Any]:
    """A run as a dict without its unbounded text fields, plus flags saying what was dropped.

    The flags matter: a caller must be able to tell "this run produced no final message" from
    "the message exists and this view omitted it", or an empty `text` reads as a fact about the
    run rather than about the view.
    """
    data = rec.model_dump(mode="json")
    for field in _BULKY_FIELDS:
        value = data.pop(field, None)
        data[f"has_{field}"] = bool(value)
    return data


def filter_runs(
    records: Sequence[RunRecord],
    *,
    status: str | None = None,
    task_id: str | None = None,
    since: datetime | None = None,
) -> list[RunRecord]:
    """Records matching every supplied filter, newest first (undated runs sort last).

    ``since`` compares against the run's start; a record with no parseable ``started_at`` is kept,
    because an unreadable timestamp is not evidence the run falls outside the window.
    """
    def in_window(rec: RunRecord) -> bool:
        if since is None:
            return True
        started = _started_at_or_none(rec)
        # An unreadable timestamp is not evidence the run falls outside the window.
        return started is None or started >= since

    out = [
        r for r in records
        if (status is None or r.status == status)
        and (task_id is None or r.task_id == task_id)
        and in_window(r)
    ]
    # Undated runs sort last in a newest-first list, so they never displace a dated run from a
    # `limit`. `datetime.min` only orders them; it is never shown as a time.
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    out.sort(key=lambda r: _started_at_or_none(r) or oldest, reverse=True)
    return out


def _started_at_or_none(rec: RunRecord) -> datetime | None:
    if not rec.started_at:
        return None
    try:
        parsed = datetime.fromisoformat(rec.started_at)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class FleetState:
    """Per-run JSON files under a directory; per-run-locked writes, aggregated on read."""

    def __init__(self, runs_dir: Path | str) -> None:
        self.dir = Path(runs_dir)
        # One lock per run_id serializes the read-modify-write in update()/update_if() so a run's
        # terminal stamp and a concurrent cancel can't lose each other's update.
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _path(self, run_id: str) -> Path:
        # The single funnel through which a run_id becomes a filesystem path (get/update/
        # update_if/add all compose here). Validated fail-closed: a traversal id must never
        # resolve outside this dir, and it raises before any path is composed.
        return self.dir / f"{validate_run_id(run_id)}.json"

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(run_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[run_id] = lock
            return lock

    def _lock_path(self, run_id: str) -> Path:
        # Sidecar next to the record: ``list``/clean only glob ``*.json``, so this never looks like
        # a run. flock auto-releases on process death - no stale-lock reaper needed.
        return self.dir / f"{validate_run_id(run_id)}.json.lock"

    @contextlib.contextmanager
    def _run_file_lock(self, run_id: str) -> Iterator[None]:
        """Exclusive ``flock`` for the run-record read-modify-write (cross-process).

        Held only for the critical section in ``update``/``update_if`` - never across a backend
        run. Pairs with ``_lock_for`` (thread lock first, then flock) so in-process callers still
        serialize cheaply and two processes on one repo cannot interleave the RMW.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        # Open+hold for the critical section; close releases the flock (incl. on crash).
        with open(self._lock_path(run_id), "a+", encoding="utf-8") as guard:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)

    def _write(self, record: RunRecord) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(record.run_id)
        # A UNIQUE temp in the same dir (not a fixed "<id>.json.tmp"), so two concurrent writers to
        # the same run can't remove each other's temp and crash the os.replace.
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=f"{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                # `agent_alive` is a live probe, never a fact about the past: writing it would put
                # a value in the ledger that is stale the instant it lands. It is recomputed by
                # whoever reads the record.
                fh.write(record.model_dump_json(indent=2, exclude=_DERIVED_FIELDS))
            os.replace(tmp, path)  # atomic: a reader sees either the old file or the whole new one
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def add(self, record: RunRecord) -> None:
        self._write(record)

    def update(self, run_id: str, **fields: Any) -> RunRecord:
        """Merge fields into a run and persist it. Serialized per run_id (thread + flock)."""
        with self._lock_for(run_id), self._run_file_lock(run_id):
            return self._update_locked(run_id, fields)

    def update_if(
        self, run_id: str, predicate: Callable[[RunRecord], bool], **fields: Any
    ) -> RunRecord:
        """Merge fields only if ``predicate(current)`` holds; return the (possibly unchanged) record.

        Read-check-write under the per-run thread lock and cross-process flock, so a conditional
        transition (e.g. cancel: "set cancelled only if still running") can't be raced by a
        concurrent terminal stamp in this process or another.
        """
        with self._lock_for(run_id), self._run_file_lock(run_id):
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
        # Test-only: widen the RMW window so concurrent writers can race without flock.
        if _rmw_between_read_write is not None:
            _rmw_between_read_write()
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
        skipped = 0
        for path in sorted(self.dir.glob("*.json")):
            try:
                records.append(RunRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (ValidationError, OSError, ValueError):
                # Skip a torn/foreign/binary file (ValueError covers UnicodeDecodeError) rather
                # than failing the whole listing; warn with the count so operators can find it.
                skipped += 1
                continue
        if skipped:
            print(
                f"[marshal] skipping {skipped} unreadable run record(s)",
                file=sys.stderr,
            )
        return records
