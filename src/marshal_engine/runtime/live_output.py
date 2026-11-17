"""Where a running agent's stdout and stderr go: files on disk, written by the child directly.

Output used to travel through pipes and live in the supervisor's memory until the agent exited.
A supervisor that died mid-run took all of it along - the session id the backend printed in its
first event, the usage it had reported so far, every line of diagnostics - and the reaper could
only stamp the run ``failed`` with nothing to show for it.

Now the child's stdout and stderr ARE two files, ``<stem>.stdout`` and ``<stem>.stderr``. The
child writes them itself, so they keep filling whether or not anyone is supervising, and whatever
was written before a crash is still there to parse.

These files are raw, unredacted output, so they are created ``0600`` and are short-lived: the
Fleet folds them into the redacted run log once the run finishes and then removes them (see
``RunLogStore``). With no stem - a backend run outside a Fleet - they are temporary files that are
unlinked as soon as the child holds them, so nothing outlives the read.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

#: Owner read/write only. The files hold whatever the agent printed, credentials included,
#: until the redacted log replaces them.
_LIVE_MODE = 0o600

STDOUT_SUFFIX = ".stdout"
STDERR_SUFFIX = ".stderr"


def live_paths(stem: Path) -> tuple[Path, Path]:
    """The stdout and stderr paths for a live-output stem."""
    return stem.with_name(stem.name + STDOUT_SUFFIX), stem.with_name(stem.name + STDERR_SUFFIX)


def read_live(stem: Path) -> tuple[str, str] | None:
    """What a run has written so far, or None when neither file exists. Never raises."""
    out_path, err_path = live_paths(stem)
    if not out_path.exists() and not err_path.exists():
        return None
    return _read_text(out_path), _read_text(err_path)


def remove_live(stem: Path) -> bool:
    """Delete both live files if present. True if anything was removed. Never raises."""
    removed = False
    for path in live_paths(stem):
        try:
            path.unlink()
            removed = True
        except OSError:
            continue
    return removed


@dataclass
class LiveOutput:
    """The two files one attempt writes to, plus the handles used to read them back.

    Reads go through handles of their own, opened separately from the ones the child inherits.
    A forked child SHARES the file offset of the descriptor it was handed, so seeking that one to
    read would move the point where a still-running agent writes next.
    """

    stdout_fh: IO[bytes]
    stderr_fh: IO[bytes]
    _stdout_reader: IO[bytes]
    _stderr_reader: IO[bytes]
    stem: Path | None

    @classmethod
    def open(cls, stem: Path | None) -> LiveOutput:
        """Create (truncating) both files and their read handles.

        Truncating is deliberate: a retried attempt writes over the attempt before it, which the
        Fleet already holds in memory and writes to the run log with every other attempt.
        """
        private_dir: Path | None = None
        if stem is None:
            # A private 0700 directory, not sibling names in the shared temp dir. Names next to a
            # mkstemp file are not reserved, so another local user could pre-create `.stdout` as a
            # symlink and have O_TRUNC destroy whatever it points at.
            private_dir = Path(tempfile.mkdtemp(prefix="marshal-live-"))
            base = private_dir / "out"
        else:
            stem.parent.mkdir(parents=True, exist_ok=True)
            base = stem
        out_path, err_path = live_paths(base)
        handles: list[IO[bytes]] = []
        try:
            for path in (out_path, err_path):
                # O_NOFOLLOW: a symlink planted at this name is refused, never followed and truncated.
                fd = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, _LIVE_MODE
                )
                # O_CREAT's mode is masked by umask and ignored for a file that already existed;
                # pin it so a pre-existing world-readable file cannot carry the raw stream.
                os.fchmod(fd, _LIVE_MODE)
                handles.append(os.fdopen(fd, "wb"))
            for path in (out_path, err_path):
                handles.append(open(path, "rb"))  # noqa: SIM115 - closed by close()
        except BaseException:
            for fh in handles:
                with contextlib.suppress(OSError):
                    fh.close()
            raise
        finally:
            if private_dir is not None:
                # Every handle that will ever exist is open, so the names can go now. The data
                # stays reachable through the handles and disappears with the last one.
                remove_live(base)
                with contextlib.suppress(OSError):
                    private_dir.rmdir()
        return cls(handles[0], handles[1], handles[2], handles[3], stem)

    def close_parent_handles(self) -> None:
        """Close the write handles in this process once the child holds its own copies."""
        for fh in (self.stdout_fh, self.stderr_fh):
            with contextlib.suppress(OSError):
                fh.close()

    def read(self) -> tuple[str, str]:
        """Everything written so far, decoded leniently. Closes the read handles.

        Called once per attempt, after the child exited or was killed.
        """
        try:
            return _decode(self._stdout_reader.read()), _decode(self._stderr_reader.read())
        finally:
            for fh in (self._stdout_reader, self._stderr_reader):
                with contextlib.suppress(OSError):
                    fh.close()

    def discard(self) -> None:
        """Close every handle and remove the files (a spawn that never happened)."""
        for fh in (self.stdout_fh, self.stderr_fh, self._stdout_reader, self._stderr_reader):
            with contextlib.suppress(OSError):
                fh.close()
        if self.stem is not None:
            remove_live(self.stem)


def _decode(data: bytes) -> str:
    # `text=True` on Popen used the locale encoding with strict errors, so one bad byte from an
    # agent raised inside communicate. A diagnostic stream is worth more with a replacement
    # character in it than not at all. Newlines are translated the way `text=True` did, so every
    # backend parser sees the same line endings it always has.
    return data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def _read_text(path: Path) -> str:
    try:
        return _decode(path.read_bytes())
    except OSError:
        return ""
