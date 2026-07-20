"""Tests for the durable per-run log store (one file per run under a directory)."""

from __future__ import annotations

import os

import pytest

from marshal_engine.logs import RunLogStore


def test_write_then_read_round_trips_full_content(tmp_path) -> None:
    # No truncation: the full raw stdout/stderr round-trips, not the 16KB-truncated `text`
    # on the run record. A driver can grep the file after the fact for anything that was said.
    store = RunLogStore(tmp_path / "logs")
    out = "first line\nsecond line\n" + ("x" * 200_000)  # well past any truncation
    err = "warning: hi\n" + ("y" * 200_000)
    store.write("r1", out, err)

    text = store.read("r1")
    assert text is not None
    assert "=== run r1 ===" in text
    assert "--- stdout ---" in text
    assert "--- stderr ---" in text
    assert out in text
    assert err in text


def test_path_returns_expected_location(tmp_path) -> None:
    # `path()` is the same path `write()` writes to - drivers can use it to stream or stat.
    store = RunLogStore(tmp_path / "logs")
    assert store.path("abc") == tmp_path / "logs" / "abc.log"


def test_read_absent_returns_none(tmp_path) -> None:
    # The MCP tool and the CLI both treat None / exit-code-1 as "no log for this id" - the
    # default must be a clean None, NOT a FileNotFoundError that would crash the call.
    assert RunLogStore(tmp_path / "logs").read("never-written") is None


def test_dir_is_created_on_first_write(tmp_path) -> None:
    # The store never assumes its directory exists (MarshalService construction is the natural
    # hook, but a Fleet wired in a test or a library caller can land here cold).
    store = RunLogStore(tmp_path / "logs" / "nested")
    store.write("r1", "hi", "")
    assert (tmp_path / "logs" / "nested" / "r1.log").exists()


def test_write_is_overwrite_semantics(tmp_path) -> None:
    # Same as FleetState.add: a second write replaces the file atomically. A run never re-logs,
    # but the contract is documented so a future re-run of the same id doesn't surprise anyone.
    store = RunLogStore(tmp_path / "logs")
    store.write("r1", "first", "")
    store.write("r1", "second", "")
    text = store.read("r1")
    assert text is not None
    assert "first" not in text
    assert "second" in text


def test_unsafe_run_id_is_rejected(tmp_path) -> None:
    # Defense-in-depth: a run_id containing a path separator would let a malformed id write
    # outside the logs dir. Run ids in production are `task.backend.<uuid8>` and safe, but the
    # store never trusts its caller. Locks the contract that file paths stay inside self.dir.
    store = RunLogStore(tmp_path / "logs")
    for bad in ("../etc", "..", ".", "a/b", "a\\b", ""):
        with pytest.raises(ValueError):
            store.path(bad)
        with pytest.raises(ValueError):
            store.read(bad)
        with pytest.raises(ValueError):
            store.write(bad, "x", "y")


def test_write_cleans_temp_file_on_replace_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirror the FleetState._write invariant: a failed atomic write must not leave a stray
    # <id>.log.<rand>.tmp in the logs dir. A driver listing the dir after a crash would see
    # only the surviving final file.
    store = RunLogStore(tmp_path / "logs")
    real_replace = os.replace

    def _boom(src: str, dst: str) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        store.write("r1", "x", "y")
    monkeypatch.setattr(os, "replace", real_replace)  # restore for the assertion below

    temps = [p for p in (tmp_path / "logs").iterdir() if p.name.startswith("r1.log.") and p.name.endswith(".tmp")]
    assert temps == []  # cleanup branch ran and unlinked the temp
