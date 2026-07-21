"""Shared test fixtures."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def fake_cursor_agent(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], Path]:
    """Install a fake ``cursor-agent`` executable on PATH; returns the installer.

    The installer takes the Python source of the fake agent (run with the worktree as cwd,
    exactly like the real CLI) and prepends its bin dir to PATH so ``CursorBackend``'s real
    ``run()`` / ``prepare()`` / ``parse_output()`` composition is exercised end-to-end with
    no network and no real Cursor install.
    """

    def _install(body: str) -> Path:
        bindir = tmp_path_factory.mktemp("fake-cursor-bin")
        impl = bindir / "impl.py"
        impl.write_text(body, encoding="utf-8")
        shim = bindir / "cursor-agent"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        return bindir

    return _install


@pytest.fixture(autouse=True)
def _hermetic_workspaces_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the central workspace registry at a per-test, nonexistent path by default.

    Anything that resolves workspaces from ``os.environ`` (``WorkspaceRegistry.from_env()``,
    ``resolve_workspaces()`` with no explicit env) would otherwise read the developer's real
    ``~/.marshal/workspaces.yaml`` and make the suite machine-dependent. A test that needs a real
    registry file just ``monkeypatch.setenv`` this var to its own path, overriding this default.
    """
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(tmp_path / "_no_registry_for_tests.yaml"))
