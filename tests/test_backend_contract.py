"""Shared backend contract — every registry adapter must honour CodingAgentBackend.

Parametrised over ``registry.backend_names()`` so a newly registered backend is covered
automatically; forgetting to override ``available_models`` fails this suite.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.core.types import (
    ModelCatalog,
    ModelSource,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageSource,
)
from marshal_engine.orchestration.registry import backend_names, make_backend

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


def _run_ast(name: str) -> ast.AST:
    """The adapter's `run()` as a syntax tree, dedented so it parses standalone."""
    src = textwrap.dedent(inspect.getsource(type(make_backend(name)).__dict__["run"]))
    return ast.parse(src)


def _calls_super_run(tree: ast.AST) -> bool:
    """True if the body contains a real `super().run(...)` call node.

    AST rather than a substring search: `"super().run("` also matches a mention in a comment,
    a docstring, or a string literal, so a hand-rolled override could pass by talking about
    delegation without doing it. This still cannot prove the call is *reached* on every path -
    that is what the behavioural test below is for, on the adapters that exist today.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Call)
            and isinstance(func.value.func, ast.Name)
            and func.value.func.id == "super"
        ):
            return True
    return False


def _spawn_calls(tree: ast.AST) -> list[str]:
    """Names of any process-spawning calls made directly in the override."""
    spawners = {"Popen", "run", "call", "check_call", "check_output", "spawnv", "spawnvp", "system"}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in spawners:
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id in {"subprocess", "os", "sp", "_sp"}:
            found.append(f"{owner.id}.{node.func.attr}")
    return found


def test_a_run_override_must_delegate_to_the_base_loop() -> None:
    """An adapter may wrap `run()`, but it may not replace it.

    Checked on the source rather than by behaviour because the failure this prevents is a new
    adapter hand-rolling `subprocess.Popen` with its own timeout - which looks fine in that
    adapter's own tests and quietly drops the process-group kill for its runs only.
    """
    for name in _run_overriding_names():
        tree = _run_ast(name)
        assert _calls_super_run(tree), (
            f"{name}: run() must delegate to CodingAgentBackend.run - the external timeout and "
            "process-group kill live there and must not be reimplemented per adapter"
        )
        assert not _spawn_calls(tree), (
            f"{name}: run() spawns a process itself ({', '.join(_spawn_calls(tree))}); "
            "wrap super().run() instead"
        )


#: Per-adapter setup needed to drive the REAL `run()` against a fake binary: redirect any
#: host-global state the adapter's transaction touches, so the test cannot write to the machine
#: it runs on. Keyed by backend name and consulted exhaustively - a `run()`-overriding adapter
#: with no entry FAILS rather than running unisolated, because the silent-skip version of this
#: would let a future adapter's transaction touch real user state during the suite.
_RUN_ISOLATION: dict[str, Callable[[CodingAgentBackend, Path], None]] = {
    # Cursor's transaction is confined to `.cursor/cli.json` under opts.cwd (already a tmp dir).
    "cursor": lambda backend, tmp_path: None,
    # Antigravity edits a host-global settings file; point it somewhere disposable.
    "antigravity": lambda backend, tmp_path: setattr(
        backend, "settings_path", tmp_path / "settings.json"
    ),
}


def _isolate(backend: CodingAgentBackend, tmp_path: Path) -> None:
    isolate = _RUN_ISOLATION.get(backend.name)
    assert isolate is not None, (
        f"{backend.name} overrides run() but has no _RUN_ISOLATION entry; add one saying what "
        "host state its transaction touches (or an explicit no-op) before it is exercised here"
    )
    isolate(backend, tmp_path)


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


def test_no_adapter_overrides_compose_prompt(backend: CodingAgentBackend) -> None:
    """Prompt composition is shared; the only per-backend choice is `resolves_at_mentions`.

    Cursor and Goose used to carry byte-identical overrides that differed from the base in one
    line. Two copies means a change to the shared `read_paths` wording lands in one and is
    forgotten in the other, and the divergence is invisible - both still "work".
    """
    assert "_compose_prompt" not in type(backend).__dict__, (
        f"{backend.name}: set `resolves_at_mentions` instead of overriding _compose_prompt"
    )


def test_context_files_reach_the_prompt_in_this_backend_s_syntax(
    backend: CodingAgentBackend,
) -> None:
    task = TaskSpec(id="t1", goal="do it", context_files=["src/a.py"])
    prompt = backend._compose_prompt(task)
    assert "src/a.py" in prompt, f"{backend.name}: context_files never reached the prompt"
    if backend.resolves_at_mentions:
        assert "@src/a.py" in prompt
    else:
        # A literal @mention to a CLI that does not resolve them is just noise in the prompt.
        assert "@src/a.py" not in prompt


def test_read_paths_notice_is_identical_across_backends() -> None:
    """The notice is one string in one place - this fails if an adapter reintroduces its own."""
    task = TaskSpec(id="t1", goal="g", read_paths=["/etc/hosts"])
    notices = {make_backend(n)._compose_prompt(task) for n in _BACKEND_NAMES}
    assert len(notices) == 1, "backends disagree on the read-only reference wording"
