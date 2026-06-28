"""Tests for child-process environment hygiene (the VIRTUAL_ENV scrub)."""

from __future__ import annotations

import pytest

from marshal_engine.env import child_env


def test_child_env_strips_driver_venv_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/driver/.venv")
    monkeypatch.setenv("PYTHONHOME", "/driver/python")
    env = child_env()
    assert "VIRTUAL_ENV" not in env   # driver's venv pin removed so the worktree's own wins
    assert "PYTHONHOME" not in env


def test_child_env_preserves_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("VIRTUAL_ENV", "/driver/.venv")
    env = child_env()
    assert env["PATH"] == "/usr/bin:/bin"  # PATH must survive - uv/git/the CLIs need it


def test_child_env_extra_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/driver/.venv")
    env = child_env({"VIRTUAL_ENV": "/wanted/.venv", "MARSHAL_X": "1"})
    assert env["VIRTUAL_ENV"] == "/wanted/.venv"  # caller can deliberately set it back
    assert env["MARSHAL_X"] == "1"
