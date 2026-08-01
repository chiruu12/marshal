"""Shared backend contract — every registry adapter must honour CodingAgentBackend.

Parametrised over ``registry.backend_names()`` so a newly registered backend is covered
automatically; forgetting to override ``available_models`` fails this suite.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.backends.goose import GooseBackend
from marshal_engine.orchestration.registry import backend_names, make_backend
from marshal_engine.core.types import (
    ModelCatalog,
    ModelSource,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageSource,
)

_BACKEND_NAMES = backend_names()


@pytest.fixture(params=_BACKEND_NAMES, ids=_BACKEND_NAMES)
def backend(request: pytest.FixtureRequest) -> CodingAgentBackend:
    return make_backend(request.param)


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_registry_param_covers_all_adapters() -> None:
    assert len(_BACKEND_NAMES) >= 7
    for name in _BACKEND_NAMES:
        assert isinstance(make_backend(name), CodingAgentBackend)


def test_available_models_is_overridden(backend: CodingAgentBackend) -> None:
    cls = type(backend)
    assert "available_models" in cls.__dict__, (
        f"{cls.__name__} must override available_models (base reports UNAVAILABLE)"
    )


def test_build_invocation_is_pure(
    backend: CodingAgentBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError(f"{backend.name}: build_invocation must not spawn a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    mode = next(iter(sorted(backend.capabilities.permission_modes, key=lambda m: m.value)))
    task = TaskSpec(id="t1", goal="hello")
    opts = _opts(permission=mode)
    first = backend.build_invocation(task, opts)
    second = backend.build_invocation(task, opts)
    assert first == second
    assert first and first[0] == backend.binary
    assert all(isinstance(x, str) for x in first)


def test_map_permission_declared_and_undeclared(backend: CodingAgentBackend) -> None:
    for mode in backend.capabilities.permission_modes:
        flags = backend.map_permission(mode)
        assert isinstance(flags, list)
        assert all(isinstance(x, str) for x in flags)
    for mode in PermissionMode:
        if mode in backend.capabilities.permission_modes:
            continue
        with pytest.raises(ValueError):
            backend.map_permission(mode)


def test_parse_output_never_raises(backend: CodingAgentBackend) -> None:
    huge = "{" + ("x" * 200_000)
    cases = (
        ("", "", 0),
        ("not json at all", "boom", 1),
        ('{"type":"partial"', "", 0),
        (huge, "", 0),
    )
    for stdout, stderr, code in cases:
        result = backend.parse_output(stdout, stderr, code)
        assert result is not None


def test_available_models_nonempty_strings(
    backend: CodingAgentBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the no-binary path so CI without CLIs still exercises the static fallback.
    monkeypatch.setattr("shutil.which", lambda _name: None)
    catalog = backend.available_models()
    assert isinstance(catalog, ModelCatalog)
    assert len(catalog.models) >= 1
    assert all(isinstance(m, str) and m.strip() for m in catalog.models)


def test_a_curated_fallback_never_claims_to_be_a_live_answer(
    backend: CodingAgentBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no CLI on PATH, nothing reported can be `PROBED` - it is a curated list.

    This is the property the bare-list return type could not express: an adapter that fell back
    to `docs/model-playbook.md` because its binary was absent looked exactly like one whose CLI
    had just answered. A driver reading that list could route at a model the account cannot run.
    """
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert backend.available_models().source is ModelSource.STATIC


def test_available_models_never_raises(
    backend: CodingAgentBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe is a convenience; it must degrade, never take down the caller's listing."""
    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("probe exploded")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/fake")
    monkeypatch.setattr(subprocess, "run", _boom)
    catalog = backend.available_models()
    assert catalog.source is not ModelSource.PROBED


def test_usage_source_in_vocabulary(backend: CodingAgentBackend) -> None:
    for stdout, code in (("", 0), ("", 1), ('{"type":"result"}\n', 0)):
        result = backend.parse_output(stdout, "", code)
        if result.usage is not None:
            assert result.usage.source in UsageSource


# --- the run() escape hatch ------------------------------------------------------------------
#
# `base.run()` is the single chokepoint for Marshal's non-negotiable invariant: every agent run
# gets an external timeout and a process-GROUP kill. Two adapters override `run()` — Cursor wraps
# it in a `.cursor/cli.json` snapshot/restore transaction, Antigravity in a `trustedWorkspaces`
# grant/release transaction. Both are setup/teardown around `super().run()`, and that is the only
# acceptable shape: an override that spawned its own process would be a second, unverified copy
# of the invariant.


def _run_overriding_names() -> list[str]:
    return [n for n in _BACKEND_NAMES if "run" in type(make_backend(n)).__dict__]


def test_a_run_override_must_delegate_to_the_base_loop() -> None:
    """An adapter may wrap `run()`, but it may not replace it.

    Checked on the source rather than by behaviour because the failure this prevents is a new
    adapter hand-rolling `subprocess.Popen` with its own timeout - which looks fine in that
    adapter's own tests and quietly drops the process-group kill for its runs only.
    """
    for name in _run_overriding_names():
        src = inspect.getsource(type(make_backend(name)).__dict__["run"])
        assert "super().run(" in src, (
            f"{name}: run() must delegate to CodingAgentBackend.run - the external timeout and "
            "process-group kill live there and must not be reimplemented per adapter"
        )
        for forbidden in ("subprocess.Popen", "subprocess.run", "os.spawn"):
            assert forbidden not in src, (
                f"{name}: run() spawns a process itself ({forbidden}); wrap super().run() instead"
            )


#: Per-adapter setup needed to drive the REAL `run()` against a fake binary: redirect any
#: host-global state the adapter's transaction touches. A `run()`-overriding adapter with no
#: entry here fails the test below rather than being silently skipped.
def _isolate(backend: CodingAgentBackend, tmp_path: Path) -> None:
    if backend.name == "antigravity":
        backend.settings_path = tmp_path / "settings.json"  # never touch the real user settings


@pytest.mark.parametrize("name", _run_overriding_names(), ids=_run_overriding_names())
def test_the_timeout_invariant_survives_a_run_override(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive a `run()`-overriding adapter's real loop against a binary that never exits.

    The base loop has its own timeout test, but it runs against a dummy backend - so nothing
    proved the invariant still held *through* Cursor's and Antigravity's wrappers. A teardown
    that swallowed the result, or a `finally` that raised, would leave the base's guarantees
    intact and this adapter's runs unprotected.
    """
    import os
    import signal
    import sys

    from marshal_engine.backends import base as base_mod

    backend = make_backend(name)
    _isolate(backend, tmp_path)

    # A fake binary under the adapter's own name that ignores argv and never exits.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / backend.binary
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -c "import time; time.sleep(30)"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    killed: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def _spy_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(base_mod.os, "killpg", _spy_killpg)

    pids: list[int] = []
    result = backend.run(
        TaskSpec(id="t1", goal="hang"),
        RunOpts(cwd=tmp_path, permission=PermissionMode.SAFE_EDIT, timeout_s=1, on_pid=pids.append),
    )

    assert result.status is RunStatus.TIMED_OUT, f"{name}: {result.status} / {result.error}"
    assert pids, f"{name}: on_pid never fired"
    assert any(sig == signal.SIGTERM for _, sig in killed), f"{name}: group never SIGTERM'd"
    assert all(pgid == pids[0] for pgid, _ in killed), f"{name}: signalled the wrong group"
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)  # reaped, not left running after TIMED_OUT


def test_goose_compose_prompt_matches_base_signature() -> None:
    base_params = list(inspect.signature(CodingAgentBackend._compose_prompt).parameters)
    goose_params = list(inspect.signature(GooseBackend._compose_prompt).parameters)
    assert goose_params == base_params == ["self", "task"]
    assert not isinstance(GooseBackend.__dict__.get("_compose_prompt"), staticmethod)
