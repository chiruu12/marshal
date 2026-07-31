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
from marshal_engine.core.types import PermissionMode, RunOpts, TaskSpec, UsageSource

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
        f"{cls.__name__} must override available_models (base returns None)"
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
    models = backend.available_models()
    assert isinstance(models, list)
    assert len(models) >= 1
    assert all(isinstance(m, str) and m.strip() for m in models)


def test_usage_source_in_vocabulary(backend: CodingAgentBackend) -> None:
    for stdout, code in (("", 0), ("", 1), ('{"type":"result"}\n', 0)):
        result = backend.parse_output(stdout, "", code)
        if result.usage is not None:
            assert result.usage.source in UsageSource


def test_goose_compose_prompt_matches_base_signature() -> None:
    base_params = list(inspect.signature(CodingAgentBackend._compose_prompt).parameters)
    goose_params = list(inspect.signature(GooseBackend._compose_prompt).parameters)
    assert goose_params == base_params == ["self", "task"]
    assert not isinstance(GooseBackend.__dict__.get("_compose_prompt"), staticmethod)
