"""Tests for `marshal drift` (fake backends; no CLI is ever spawned)."""

from __future__ import annotations

import subprocess

import pytest

from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.core.types import (
    AgentResult,
    Capabilities,
    ModelCatalog,
    ModelSource,
    PermissionMode,
    RunOpts,
    TaskSpec,
)
from marshal_engine.interfaces.drift import (
    FAIL,
    INFO,
    OK,
    WARN,
    detect_drift,
)


class _FakeBackend(CodingAgentBackend):
    """Backend whose version line and live catalogue are both dictated by the test."""

    def __init__(
        self,
        name: str,
        *,
        version: str | None,
        verified: str | None = None,
        static: tuple[str, ...] = (),
        catalog: ModelCatalog | None = None,
    ) -> None:
        self.name = name
        self.binary = name
        self.capabilities = Capabilities()
        self.static_models = static
        self.verified_version = verified
        self._version = version
        self._catalog = catalog or ModelCatalog()

    def probe_version(self) -> str | None:
        return self._version

    def available_models(self) -> ModelCatalog:
        return self._catalog

    def check_available(self) -> bool:
        return self._version is not None

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [self.binary]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult:
        raise NotImplementedError


def _probed(*models: str) -> ModelCatalog:
    return ModelCatalog(models=list(models), source=ModelSource.PROBED)


def _finding(report, backend: str, kind: str):
    return next(f for f in report.findings if f.backend == backend and f.kind == kind)


# --- what gets checked at all ---------------------------------------------------------------


def test_a_backend_whose_cli_is_absent_is_skipped_not_failed():
    """Drift says nothing about a CLI you never installed - that is doctor's job, not this one."""
    report = detect_drift({"ghost": _FakeBackend("ghost", version=None)})
    assert report.checked == []
    assert report.skipped == ["ghost"]
    assert report.findings == []
    assert report.ok is True


def test_backends_are_reported_in_a_stable_order():
    report = detect_drift(
        {
            "zeta": _FakeBackend("zeta", version="1.0", verified="1.0"),
            "alpha": _FakeBackend("alpha", version="1.0", verified="1.0"),
        }
    )
    assert report.checked == ["alpha", "zeta"]


# --- version drift --------------------------------------------------------------------------


def test_matching_version_is_ok():
    report = detect_drift({"a": _FakeBackend("a", version="1.1.15", verified="1.1.15")})
    f = _finding(report, "a", "version")
    assert f.status == OK
    assert report.warns == 0


def test_a_moved_cli_warns_and_names_both_builds():
    report = detect_drift({"a": _FakeBackend("a", version="1.2.0", verified="1.1.15")})
    f = _finding(report, "a", "version")
    assert f.status == WARN
    assert "1.2.0" in f.detail and "1.1.15" in f.detail
    assert report.warns == 1


def test_version_drift_alone_never_fails_the_command():
    """CLIs that ship nightly would otherwise be red every day, and a red check stops being read."""
    report = detect_drift({"a": _FakeBackend("a", version="9.9.9", verified="1.0.0")})
    assert report.fails == 0
    assert report.ok is True


def test_a_missing_baseline_is_info_not_agreement():
    report = detect_drift({"a": _FakeBackend("a", version="1.1.15", verified=None)})
    f = _finding(report, "a", "version")
    assert f.status == INFO
    assert "no verified baseline" in f.detail
    assert 'verified_version = "1.1.15"' in f.fix


def test_a_notice_is_never_offered_as_a_baseline_to_record():
    """A CLI caught mid-self-update answered --version with only `Updated 1.26.0 -> 1.28.1`.

    Warning is right - the build did move - but recording that line as the baseline would leave a
    permanent mismatch, so the fix must send you back to the CLI instead.
    """
    report = detect_drift(
        {"a": _FakeBackend("a", version="Updated 1.26.0 -> 1.28.1", verified="1.26.0")}
    )
    f = _finding(report, "a", "version")
    assert f.status == WARN
    assert "verified_version" not in f.fix
    assert "--version" in f.fix


def test_a_plain_version_is_offered_as_a_baseline_to_record():
    report = detect_drift({"a": _FakeBackend("a", version="1.28.1", verified="1.26.0")})
    assert 'verified_version = "1.28.1"' in _finding(report, "a", "version").fix


def test_a_version_line_carrying_a_product_name_still_counts_as_one_version():
    report = detect_drift(
        {"a": _FakeBackend("a", version="GitHub Copilot CLI 1.0.80.", verified=None)}
    )
    assert 'verified_version = "GitHub Copilot CLI 1.0.80."' in _finding(report, "a", "version").fix


# --- model catalogue drift ------------------------------------------------------------------


def test_a_curated_id_the_cli_dropped_fails():
    """The Antigravity bug: the fallback named ids the CLI had stopped accepting."""
    report = detect_drift(
        {
            "a": _FakeBackend(
                "a",
                version="1.0",
                verified="1.0",
                static=("gemini-3.5-flash", "claude-sonnet-4-6"),
                catalog=_probed("claude-sonnet-4-6"),
            )
        }
    )
    f = _finding(report, "a", "models")
    assert f.status == FAIL
    assert "gemini-3.5-flash" in f.detail
    assert "claude-sonnet-4-6" not in f.detail
    assert report.fails == 1
    assert report.ok is False


def test_every_dropped_id_is_named_not_just_the_first():
    report = detect_drift(
        {
            "a": _FakeBackend(
                "a", version="1.0", verified="1.0", static=("x", "y", "z"), catalog=_probed("z")
            )
        }
    )
    f = _finding(report, "a", "models")
    assert "x" in f.detail and "y" in f.detail
    assert "2 id(s)" in f.detail


def test_a_fallback_the_cli_still_offers_is_ok():
    report = detect_drift(
        {
            "a": _FakeBackend(
                "a", version="1.0", verified="1.0", static=("x", "y"), catalog=_probed("x", "y")
            )
        }
    )
    f = _finding(report, "a", "models")
    assert f.status == OK
    assert "2 checked" in f.detail


def test_a_handful_of_new_ids_is_named():
    report = detect_drift(
        {
            "a": _FakeBackend(
                "a",
                version="1.0",
                verified="1.0",
                static=("x", "y", "z"),
                catalog=_probed("x", "y", "z", "new-1", "new-2"),
            )
        }
    )
    f = _finding(report, "a", "models")
    assert f.status == INFO
    assert "new-1" in f.detail and "new-2" in f.detail


def test_named_new_ids_are_capped_and_the_rest_counted():
    static = tuple(f"s{i}" for i in range(20))
    new = tuple(f"n{i}" for i in range(10))
    report = detect_drift(
        {
            "a": _FakeBackend(
                "a", version="1.0", verified="1.0", static=static, catalog=_probed(*static, *new)
            )
        }
    )
    detail = _finding(report, "a", "models").detail
    assert "+5 more" in detail
    assert "n9" not in detail


def test_a_deliberate_shortlist_does_not_report_the_whole_catalogue():
    """Cursor curates one id out of 200+. Naming the rest every run is how a check stops being read."""
    report = detect_drift(
        {
            "a": _FakeBackend(
                "a",
                version="1.0",
                verified="1.0",
                static=("composer-2.5",),
                catalog=_probed("composer-2.5", *[f"m{i}" for i in range(50)]),
            )
        }
    )
    f = _finding(report, "a", "models")
    assert f.status == OK
    assert "50 more" in f.detail
    assert "m0" not in f.detail


def test_a_shortlist_still_fails_when_its_own_id_is_gone():
    """Suppressing catalogue growth must not suppress the finding that actually matters."""
    report = detect_drift(
        {
            "a": _FakeBackend(
                "a",
                version="1.0",
                verified="1.0",
                static=("composer-2.5",),
                catalog=_probed(*[f"m{i}" for i in range(50)]),
            )
        }
    )
    assert _finding(report, "a", "models").status == FAIL


def test_a_genuinely_probeless_adapter_claims_nothing_and_stays_ok():
    """STATIC means no live probe was attempted - drift claims neither pass nor fail."""
    report = detect_drift(
        {
            "a": _FakeBackend(
                "a",
                version="1.0",
                verified="1.0",
                static=("x",),
                catalog=ModelCatalog(models=["x"], source=ModelSource.STATIC),
            )
        }
    )
    f = _finding(report, "a", "models")
    assert f.status == INFO
    assert "cannot be checked" in f.detail
    assert report.warns == 0
    assert report.ok is True


def test_a_failed_models_probe_on_an_installed_cli_is_not_ok():
    """Version answered but the catalogue could not be verified - must not read as clean.

    This is the defect class the check exists to catch: an installed CLI whose curated fallback
    names a removed id would otherwise report ok while a degraded run fails at runtime.
    """
    report = detect_drift(
        {
            "a": _FakeBackend(
                "a",
                version="1.0",
                verified="1.0",
                static=("x",),
                catalog=ModelCatalog(models=["x"], source=ModelSource.PROBE_FAILED),
            )
        }
    )
    f = _finding(report, "a", "models")
    assert f.status == WARN
    assert "models probe failed" in f.detail
    assert "not verified" in f.detail
    assert "real run" in f.fix
    assert report.warns == 1
    assert report.fails == 0
    assert report.ok is False


def test_probe_models_failure_after_version_answers_is_not_reported_clean(monkeypatch):
    """End-to-end: `_probe_models` must tag PROBE_FAILED so drift does not treat it as STATIC.

    Reverting either the base tag or the drift handling of that tag makes this green again wrongly.
    """

    class _ProbeCapable(CodingAgentBackend):
        name = "probe-capable"
        binary = "marshal-fake-probe-cli"
        capabilities = Capabilities()
        static_models = ("stale-model",)
        verified_version = "1.0.0"

        def probe_version(self) -> str | None:
            return "1.0.0"

        def available_models(self) -> ModelCatalog:
            return self._probe_models(
                [self.binary, "models"],
                lambda _stdout: [],
                self.static_models,
            )

        def check_available(self) -> bool:
            return True

        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            return [self.binary]

        def map_permission(self, mode: PermissionMode) -> list[str]:
            return []

        def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult:
            raise NotImplementedError

    monkeypatch.setattr(
        "marshal_engine.backends.base.shutil.which",
        lambda _b: "/usr/bin/marshal-fake-probe-cli",
    )
    monkeypatch.setattr(
        "marshal_engine.backends.base.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "boom"),
    )
    backend = _ProbeCapable()
    assert backend.available_models().source is ModelSource.PROBE_FAILED

    report = detect_drift({"probe-capable": backend})
    f = _finding(report, "probe-capable", "models")
    assert f.status == WARN
    assert report.ok is False


def test_a_backend_with_no_curated_fallback_reports_the_live_size():
    report = detect_drift(
        {"a": _FakeBackend("a", version="1.0", verified="1.0", catalog=_probed("x", "y"))}
    )
    f = _finding(report, "a", "models")
    assert f.status == INFO
    assert "2 model(s)" in f.detail


# --- the real registry ----------------------------------------------------------------------


def test_every_shipped_backend_exposes_its_curated_fallback_to_the_check():
    """`static_models` is what drift compares against the live CLI.

    Each adapter also keeps a module-level ``_STATIC_MODELS``; the two must be the same object,
    not two lists that can quietly diverge - a class attribute that fell behind the constant the
    adapter actually degrades to would make drift check a list nobody runs on.
    """
    import importlib

    from marshal_engine.orchestration.registry import _FACTORIES

    for name, factory in _FACTORIES.items():
        cls = factory().__class__
        constant = getattr(importlib.import_module(cls.__module__), "_STATIC_MODELS")
        assert cls.static_models == constant, name
        assert cls.static_models, name


def test_zcode_probes_its_resolved_launcher_not_the_path_shim(monkeypatch, tmp_path):
    """ZCode's entry point can be an app bundle or MARSHAL_ZCODE_BIN, with no `zcode` on PATH.

    A drift check that only asked `shutil.which("zcode")` would report a perfectly installed
    backend as absent and check nothing - the same defect `resolve_launcher` exists to prevent.
    """
    from marshal_engine.backends.zcode import ZCodeBackend

    launcher = tmp_path / "zcode-real"
    launcher.write_text("#!/bin/sh\necho 0.16.3\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("MARSHAL_ZCODE_BIN", str(launcher))
    monkeypatch.setattr("marshal_engine.backends.zcode.shutil.which", lambda _b: None)

    assert ZCodeBackend().probe_version() == "0.16.3"


def test_zcode_reports_absent_when_nothing_resolves(monkeypatch):
    from marshal_engine.backends.zcode import ZCodeBackend

    monkeypatch.delenv("MARSHAL_ZCODE_BIN", raising=False)
    monkeypatch.setattr("marshal_engine.backends.zcode.shutil.which", lambda _b: None)
    monkeypatch.setattr("marshal_engine.backends.zcode._BUNDLE_CANDIDATES", ())
    assert ZCodeBackend().probe_version() is None


def test_probe_version_returns_none_when_the_binary_is_absent():
    class _Real(CodingAgentBackend):
        name = "nope"
        binary = "marshal-no-such-binary-xyz"
        capabilities = Capabilities()

        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            return []

        def map_permission(self, mode: PermissionMode) -> list[str]:
            return []

        def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult:
            raise NotImplementedError

    assert _Real().probe_version() is None


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess([], 1, "1.2.3", ""),
        subprocess.CompletedProcess([], 0, "", ""),
    ],
    ids=["non-zero-exit", "no-output"],
)
def test_probe_version_degrades_to_none_rather_than_guessing(monkeypatch, outcome):
    class _Real(CodingAgentBackend):
        name = "x"
        binary = "sh"
        capabilities = Capabilities()

        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            return []

        def map_permission(self, mode: PermissionMode) -> list[str]:
            return []

        def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult:
            raise NotImplementedError

    monkeypatch.setattr("marshal_engine.backends.base.subprocess.run", lambda *a, **k: outcome)
    assert _Real().probe_version() is None


def test_probe_version_survives_a_cli_that_cannot_be_launched(monkeypatch):
    class _Real(CodingAgentBackend):
        name = "x"
        binary = "sh"
        capabilities = Capabilities()

        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            return []

        def map_permission(self, mode: PermissionMode) -> list[str]:
            return []

        def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult:
            raise NotImplementedError

    def _boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr("marshal_engine.backends.base.subprocess.run", _boom)
    assert _Real().probe_version() is None


def test_probe_version_reads_a_cli_that_answers_on_stderr(monkeypatch):
    class _Real(CodingAgentBackend):
        name = "x"
        binary = "sh"
        capabilities = Capabilities()

        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            return []

        def map_permission(self, mode: PermissionMode) -> list[str]:
            return []

        def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult:
            raise NotImplementedError

    monkeypatch.setattr(
        "marshal_engine.backends.base.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "", "9.9.9\nextra\n"),
    )
    assert _Real().probe_version() == "9.9.9"
