"""Durable per-run log storage - the full stdout/stderr of one run, persisted to disk.

The driver (or MCP server) needs to inspect what an agent actually did after the fact. Today the
engine stamps a 16KB-truncated `text` on the run record, but the *full* subprocess output (the
whole stdout/stderr stream, not the parsed final message) lives only on the AgentResult and is
discarded. This module keeps it: one file per run under `<base>/<run_id>.log` with a clear header
(`=== run <run_id> ===`, `--- stdout ---` / content, `--- stderr ---` / content). Writes are atomic
(unique temp + `os.replace`, same idiom as FleetState) so a torn read never sees partial content;
the Fleet guards them defensively (a log write failure must never break a run).

Before persistence, known credential *values* from the parent environment are replaced with
``[redacted:VAR]`` markers (see ``env.redact_secrets``). Key-name-only filters miss ``env`` output
and ``echo $VAR``; value-based redaction covers those.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from .env import redact_secrets
from .worktree import validate_run_id


class RunLogStore:
    """One file per run under a directory; atomic writes, plain read on demand.

    `write` is the only mutator: it overwrites the whole file in one atomic step. Reads return the
    stored text or None if no log exists. The run_id goes through the shared ``validate_run_id``
    before it's used as a filename (run ids in production are `task.backend.<uuid8>` and safe;
    the store never trusts its caller).
    """

    def __init__(self, logs_dir: Path | str) -> None:
        self.dir = Path(logs_dir)

    def path(self, run_id: str) -> Path:
        """Where this run's log would be stored (does not create it)."""
        return self._path(run_id)

    def write(self, run_id: str, stdout: str, stderr: str) -> None:
        """Persist the run's stdout and stderr as a single, headed file (atomic).

        Credential values present in the parent environment are redacted before the write.
        Overwrites any prior log for the same run_id - the run has a new outcome to record.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(run_id)
        safe_out = redact_secrets(stdout)
        safe_err = redact_secrets(stderr)
        # A UNIQUE temp in the same dir, so two concurrent writes to the same run_id can't
        # remove each other's temp and crash the os.replace.
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=f"{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"=== run {run_id} ===\n")
                fh.write("--- stdout ---\n")
                fh.write(safe_out)
                fh.write("\n--- stderr ---\n")
                fh.write(safe_err)
            os.replace(tmp, path)  # atomic: a reader sees either the old file or the whole new one
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def read(self, run_id: str) -> str | None:
        """The stored log text, or None if no log has been written for this run."""
        path = self._path(run_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def remove(self, run_id: str) -> bool:
        """Delete this run's log if present (best-effort). Returns True if a file was removed.

        Never raises: an unsafe run_id or an OS error just yields False, so callers (e.g. `clean`)
        can reclaim log disk alongside worktrees without a log failure breaking the teardown.
        """
        try:
            path = self._path(run_id)
        except ValueError:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def _path(self, run_id: str) -> Path:
        return self.dir / f"{validate_run_id(run_id)}.log"
