"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_workspaces_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the central workspace registry at a per-test, nonexistent path by default.

    Anything that resolves workspaces from ``os.environ`` (``WorkspaceRegistry.from_env()``,
    ``resolve_workspaces()`` with no explicit env) would otherwise read the developer's real
    ``~/.marshal/workspaces.yaml`` and make the suite machine-dependent. A test that needs a real
    registry file just ``monkeypatch.setenv`` this var to its own path, overriding this default.
    """
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(tmp_path / "_no_registry_for_tests.yaml"))
