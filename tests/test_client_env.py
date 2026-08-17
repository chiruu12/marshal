"""Integration tests: per-client env reaches spawned agent children via run_agent/spawn/run_many."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from marshal_engine import AgentResult, Capabilities, PermissionMode, RunOpts, RunStatus, TaskSpec
from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.core.config import ClientConfig, FleetConfig, load_config
from marshal_engine.interfaces.service import MarshalService


class _EnvProbe(CodingAgentBackend):
    """Prints one env var named by TaskSpec.id (the test passes the var name as task_id)."""

    name = "envprobe"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        var = task.id
        return [
            sys.executable,
            "-c",
            f"import os; print(os.environ.get({var!r}, 'UNSET'))",
        ]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


def _init_repo(root: Path) -> None:
    def git(*a: str) -> None:
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (root / "README.md").write_text("hi")
    git("add", "-A")
    git("commit", "-q", "-m", "init")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _init_repo(r)
    return r


def _service(repo: Path, clients: dict[str, ClientConfig]) -> MarshalService:
    cfg = FleetConfig(clients=clients)
    return MarshalService(repo, cfg, backends={"envprobe": _EnvProbe()})


def test_run_agent_sets_client_env_in_child(repo: Path) -> None:
    svc = _service(
        repo,
        {"worker": ClientConfig(name="worker", backend="envprobe", env={"FOO": "bar"})},
    )
    rec = svc.run_agent("worker", goal="ignored", task_id="FOO")
    assert rec.status == "exited_clean"
    assert rec.text == "bar"


def test_spawn_sets_client_env_in_child(repo: Path) -> None:
    svc = _service(
        repo,
        {"worker": ClientConfig(name="worker", backend="envprobe", env={"FOO": "from-spawn"})},
    )
    rec = svc.spawn("worker", goal="ignored", task_id="FOO")
    assert rec.status == "running"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        live = svc.get_run(rec.run_id)
        assert live is not None
        if live.status != "running":
            assert live.status == "exited_clean"
            assert live.text == "from-spawn"
            return
        time.sleep(0.05)
    pytest.fail("spawn did not finish in time")


def test_run_many_sets_client_env_per_job(repo: Path) -> None:
    svc = _service(
        repo,
        {
            "a": ClientConfig(name="a", backend="envprobe", env={"FOO": "alpha"}),
            "b": ClientConfig(name="b", backend="envprobe", env={"FOO": "beta"}),
        },
    )
    results = svc.run_many(
        [
            {"client": "a", "goal": "x", "task_id": "FOO"},
            {"client": "b", "goal": "y", "task_id": "FOO"},
        ],
        max_concurrency=2,
    )
    by_client = {r.primary.client: r.primary.text for r in results}
    assert by_client == {"a": "alpha", "b": "beta"}


def test_two_codex_clients_do_not_leak_env(repo: Path, tmp_path: Path) -> None:
    stock = tmp_path / "codex-stock"
    east = tmp_path / "codex-eastrouter"
    stock.mkdir()
    east.mkdir()
    svc = _service(
        repo,
        {
            "codex-stock": ClientConfig(
                name="codex-stock",
                backend="envprobe",
                env={"CODEX_HOME": str(stock.resolve())},
            ),
            "codex-eastrouter": ClientConfig(
                name="codex-eastrouter",
                backend="envprobe",
                env={"CODEX_HOME": str(east.resolve())},
            ),
        },
    )
    r1 = svc.run_agent("codex-stock", goal="x", task_id="CODEX_HOME")
    r2 = svc.run_agent("codex-eastrouter", goal="y", task_id="CODEX_HOME")
    assert r1.text == str(stock.resolve())
    assert r2.text == str(east.resolve())


def test_load_config_env_reaches_child_via_service(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = str(Path("~/.codex-eastrouter").expanduser())
    cfg_path = tmp_path / "fleet.config.yaml"
    cfg_path.write_text(
        "clients:\n"
        "  router:\n"
        "    backend: envprobe\n"
        "    env:\n"
        "      CODEX_HOME: ~/.codex-eastrouter\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.clients["router"].env["CODEX_HOME"] == expected
    svc = MarshalService(
        repo, cfg, backends={"envprobe": _EnvProbe()}, config_path=cfg_path
    )
    rec = svc.run_agent("router", goal="x", task_id="CODEX_HOME")
    assert rec.text == expected


def test_a_client_env_launcher_is_honoured_on_every_availability_path(tmp_path: Path) -> None:
    """REGRESSION: `client_available` (what workflows and teams gate on) probed without the
    client's `env:`, while construction honoured it. A client whose `env:` was the only thing
    naming its launcher therefore worked for direct runs but vanished from workflow fan-outs and
    team reviews — one client, two answers, depending on which door it came through."""
    seen: list[dict[str, str] | None] = []

    class _EnvLauncher(CodingAgentBackend):
        """Runnable ONLY when the client's env names it — like ZCode with no shim or bundle."""

        name = "envlauncher"
        binary = "envlauncher"
        capabilities = Capabilities()

        def check_available(self) -> bool:
            return self.available_for_client(None)

        def available_for_client(self, client_env: dict[str, str] | None = None) -> bool:
            seen.append(client_env)
            return bool((client_env or {}).get("LAUNCHER"))

        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            return []

        def map_permission(self, mode: PermissionMode) -> list[str]:
            return []

        def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
            return AgentResult(status=RunStatus.EXITED_CLEAN)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    config = FleetConfig(
        clients={
            "pinned": ClientConfig(
                name="pinned", backend="envlauncher", env={"LAUNCHER": "/opt/x/bin"}
            )
        }
    )
    svc = MarshalService(repo, config, backends={"envlauncher": _EnvLauncher()})

    assert "pinned" not in svc.skipped_clients, "construction dropped a runnable client"
    assert svc.client_available("pinned") is True, (
        "workflows and teams would have skipped a client that direct runs accept"
    )
    assert {"LAUNCHER": "/opt/x/bin"} in seen, "the client's env never reached the probe"
