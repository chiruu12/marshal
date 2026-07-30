"""Cross-process Antigravity settings integrity (issue #144 / M11).

Kept in a dedicated module so parallel edits to ``test_antigravity_backend.py`` do not collide.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from marshal_engine.backends.antigravity import _trust_workspace


class TestCrossProcessSettingsLock:
    """Host-global trustedWorkspaces RMW must serialize across processes.

    Two Marshal processes sharing ``settings.json`` used to interleave read-modify-write and
    drop each other's trust grant. The ``settings.lock`` flock closes that hole for the
    prepare/release transaction (writes stay atomic via unique temp + replace).
    """

    def test_interleaved_trust_grants_are_not_dropped(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        # Per-holder threading.Lock (like two processes) - only the flock serializes the RMW.
        n = 24

        def trust_many(start: int) -> None:
            lock = threading.Lock()
            for i in range(start, start + n):
                wt = tmp_path / f"wt-{i}"
                wt.mkdir(exist_ok=True)
                _trust_workspace(settings, wt, lock)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(trust_many, 0), pool.submit(trust_many, n)]
            for fut in futures:
                fut.result()

        trusted = json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]
        expected = {str((tmp_path / f"wt-{i}").resolve()) for i in range(2 * n)}
        assert set(trusted) == expected, (
            f"interleaved settings RMW dropped trust grants; "
            f"missing={expected - set(trusted)}"
        )
        # Lock sidecar is durable and must not look like a settings.json temp leftover.
        assert (tmp_path / "settings.lock").exists()
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("settings.json")]
        assert leftovers == ["settings.json"]

    def test_concurrent_os_processes_do_not_drop_trust_grants(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        worker = r"""
import sys
from pathlib import Path
from marshal_engine import PermissionMode, RunOpts
from marshal_engine.backends.antigravity import AntigravityBackend

settings = Path(sys.argv[1])
base = Path(sys.argv[2])
start = int(sys.argv[3])
n = int(sys.argv[4])
backend = AntigravityBackend()
backend.settings_path = settings
for i in range(start, start + n):
    wt = base / f"wt-{i}"
    wt.mkdir(exist_ok=True)
    backend.prepare(RunOpts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
"""
        n = 20
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", worker, str(settings), str(tmp_path), str(start), str(n)],
            )
            for start in (0, n)
        ]
        for proc in procs:
            assert proc.wait(timeout=60) == 0

        trusted = json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]
        expected = {str((tmp_path / f"wt-{i}").resolve()) for i in range(2 * n)}
        assert set(trusted) == expected
        assert (tmp_path / "settings.lock").exists()
