"""Tests for MarshalService - client resolution + run recording (dummy backend, no network)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from marshal_engine import (
    AgentResult,
    Capabilities,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.config import (
    DEFAULT_OPENCODE_MODEL,
    ClientConfig,
    ConfigError,
    FleetConfig,
    FleetContext,
    load_config,
)
from marshal_engine.service import MarshalService


class _Echo(CodingAgentBackend):
    name = "echo"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            usage=UsageRecord(backend="echo", cost_usd=0.002, source=UsageSource.NATIVE),
            exit_code=exit_code,
        )


class _Pricey(CodingAgentBackend):
    """A second strategy with a higher native cost - used to compare benchmark strategies."""

    name = "pricey"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            usage=UsageRecord(backend="pricey", cost_usd=0.05, source=UsageSource.NATIVE),
            exit_code=exit_code,
        )


class _Unpriced(CodingAgentBackend):
    """A strategy with no usage info - its cost is 'unavailable', not a real $0."""

    name = "noinfo"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('done')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED, text=raw_stdout.strip(), exit_code=exit_code)


class _Capture(CodingAgentBackend):
    """Records each TaskSpec it is asked to run, so tests can assert what the service threaded through."""

    name = "capture"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self) -> None:
        self.tasks: list[TaskSpec] = []

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        self.tasks.append(task)
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED, text=raw_stdout.strip(), exit_code=exit_code)


class _Missing(CodingAgentBackend):
    """A backend whose CLI is unavailable - check_available() is always False."""

    name = "missing"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return False

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED, text=raw_stdout.strip(), exit_code=exit_code)


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


def _svc(repo: Path) -> MarshalService:
    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="echo", permission=PermissionMode.SAFE_EDIT)}
    )
    return MarshalService(repo, cfg, backends={"echo": _Echo()})


def test_list_clients(repo: Path) -> None:
    svc = _svc(repo)
    result = svc.list_clients()
    assert [c.model_dump() for c in result.clients] == [
        {"name": "worker", "backend": "echo", "model": None, "permission": "safe-edit"}
    ]
    assert result.driver_context is None  # no context.driver in this config


def test_list_clients_surfaces_driver_context(repo: Path) -> None:
    # context.driver is surfaced back to the driver on list_clients (None when unset).
    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="echo", permission=PermissionMode.SAFE_EDIT)},
        context=FleetContext(driver="Fleet runs review + impl; integrate manually."),
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    result = svc.list_clients()
    assert result.driver_context == "Fleet runs review + impl; integrate manually."
    assert [c.name for c in result.clients] == ["worker"]
def test_run_agent_records(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do something", task_id="t1")
    assert rec.status == "succeeded"
    assert rec.run_id.startswith("t1.echo.")  # task.backend.<uuid>
    assert svc.get_run(rec.run_id) is not None
    assert svc.status()[0].run_id == rec.run_id
    assert svc.usage().totals.runs == 1
    assert abs(svc.usage().totals.cost_usd - 0.002) < 1e-9


def test_run_agent_unknown_client(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError):
        svc.run_agent("nope", "x")


def _capture_svc(repo: Path, backend: _Capture, *, worker: str | None = None) -> MarshalService:
    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(name="worker", backend="capture", permission=PermissionMode.SAFE_EDIT)
        },
        context=FleetContext(worker=worker) if worker else FleetContext(),
    )
    return MarshalService(repo, cfg, backends={"capture": backend})


def test_run_agent_threads_context_files_to_the_task(repo: Path) -> None:
    # context_files is consumed by every backend's prompt; the service must carry it onto the TaskSpec
    # so a driver can actually point a worker at the files it should see.
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    svc.run_agent("worker", "do x", task_id="t1", context_files=["a.py", "b.py"])
    assert backend.tasks[-1].context_files == ["a.py", "b.py"]


def test_goal_is_prefixed_with_worker_preamble(repo: Path) -> None:
    # The worker preamble is injected into every goal, and the user's original goal survives.
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    svc.run_agent("worker", "refactor the parser", task_id="t1")
    goal = backend.tasks[-1].goal
    assert goal.startswith("You are a headless coding agent in a Marshal fleet")
    assert "headless coding agent in a Marshal fleet" in goal
    assert "refactor the parser" in goal  # the user's goal text is still present


def test_goal_includes_fleet_worker_context_when_set(repo: Path) -> None:
    # When context.worker is set, it is layered between the preamble and the user's goal.
    backend = _Capture()
    svc = _capture_svc(repo, backend, worker="Always add type hints. No new deps.")
    svc.run_agent("worker", "fix the bug", task_id="t1")
    goal = backend.tasks[-1].goal
    assert goal.startswith("You are a headless coding agent in a Marshal fleet")
    assert "Always add type hints. No new deps." in goal  # fleet worker context
    assert "fix the bug" in goal  # user's goal still present
    # ordering: preamble, then worker context, then goal
    assert goal.index("headless coding agent") < goal.index("Always add type hints")
    assert goal.index("Always add type hints") < goal.index("fix the bug")

def test_run_many_threads_context_files_per_job(repo: Path) -> None:
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    svc.run_many([{"client": "worker", "goal": "g", "task_id": "j1", "context_files": ["x.py"]}])
    assert backend.tasks[-1].context_files == ["x.py"]


def test_run_agent_does_not_stamp_client_name_into_role(repo: Path) -> None:
    # `role` is a semantic routing role, not the client name; the client is tracked separately.
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    rec = svc.run_agent("worker", "do x", task_id="t1")
    assert backend.tasks[-1].role is None
    assert rec.client == "worker"  # client identity is still recorded, just not as a "role"


def test_collect_run_surfaces_changed_files(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do something", task_id="t1")
    collected = svc.collect_run(rec.run_id)
    assert collected.run_id == rec.run_id
    assert collected.branch == rec.branch


def test_commit_run_delegates(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do something", task_id="t1")
    result = svc.commit_run(rec.run_id)
    assert result.status in ("committed", "clean")  # _Echo writes nothing -> clean
    assert result.commit  # a concrete branch-tip ref to chain on
    assert svc.get_run(rec.run_id).commit == result.commit


def test_clean_delegates(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do something", task_id="t1")  # succeeded, un-integrated
    assert svc.clean().removed == []                  # default scope protects it
    result = svc.clean(scope="all")                    # opt in to clean it
    assert rec.run_id in result.removed
    assert svc.get_run(rec.run_id) is not None         # state/history kept; only the worktree went


def _bench_svc(repo: Path, backends: dict[str, object], **clients: str) -> MarshalService:
    cfg = FleetConfig(
        clients={
            name: ClientConfig(name=name, backend=backend, permission=PermissionMode.SAFE_EDIT)
            for name, backend in clients.items()
        }
    )
    return MarshalService(repo, cfg, backends=backends)  # type: ignore[arg-type]


def test_benchmark_compares_strategies(repo: Path) -> None:
    svc = _bench_svc(repo, {"echo": _Echo(), "pricey": _Pricey()}, cheap="echo", dear="pricey")
    result = svc.benchmark("do x", ["cheap", "dear"], task_id="b1")

    assert result.task_id == "b1"
    assert {s.client for s in result.strategies} == {"cheap", "dear"}
    assert all(s.status == "succeeded" for s in result.strategies)
    assert result.cheapest == "cheap"          # 0.002 < 0.05, both costs native (known)
    assert result.fastest in {"cheap", "dear"}
    assert len({s.run_id for s in result.strategies}) == 2  # distinct runs, shared task_id


def test_report_requeries_a_past_benchmark(repo: Path) -> None:
    svc = _bench_svc(repo, {"echo": _Echo(), "pricey": _Pricey()}, cheap="echo", dear="pricey")
    svc.benchmark("do x", ["cheap", "dear"], task_id="b2")
    again = svc.report("b2")  # pure re-query from the ledger
    assert again.cheapest == "cheap"
    assert len(again.strategies) == 2


def test_benchmark_cheapest_excludes_unknown_cost(repo: Path) -> None:
    # a strategy whose cost is "unavailable" must NOT win cheapest just because it reports $0
    svc = _bench_svc(repo, {"echo": _Echo(), "noinfo": _Unpriced()}, known="echo", mystery="noinfo")
    result = svc.benchmark("x", ["known", "mystery"], task_id="b3")
    assert result.cheapest == "known"  # not "mystery", despite its $0 unavailable cost


def test_report_admin_api_cost_competes_for_cheapest(repo: Path) -> None:
    # Regression: a real EastRouter (admin-api) cost is a KNOWN cost and must be comparable for
    # `cheapest` - it was previously excluded (only native/estimated were), so a real cheaper run lost.
    from marshal_engine.state import RunRecord

    svc = _svc(repo)
    svc.fleet.state.add(
        RunRecord(run_id="b.cheap", task_id="b", backend="x", client="cheap",
                  status="succeeded", cost_usd=0.01, source="admin-api")
    )
    svc.fleet.state.add(
        RunRecord(run_id="b.dear", task_id="b", backend="x", client="dear",
                  status="succeeded", cost_usd=0.05, source="native")
    )
    result = svc.report("b")
    assert result.cheapest == "cheap"  # the admin-api run is the cheapest comparable strategy


def test_run_many_runs_each_client_job(repo: Path) -> None:
    svc = _svc(repo)
    jobs = [
        {"client": "worker", "goal": "a", "task_id": "j1"},
        {"client": "worker", "goal": "b", "task_id": "j2"},
        {"client": "worker", "goal": "c", "task_id": "j3"},
    ]
    records = svc.run_many(jobs, max_concurrency=3)
    assert [r.task_id for r in records] == ["j1", "j2", "j3"]
    assert all(r.status == "succeeded" for r in records)
    assert len(svc.status()) == 3


def test_spawn_records_running_then_finishes(repo: Path) -> None:
    svc = _svc(repo)
    try:
        rec = svc.spawn("worker", "do x", task_id="sp1")
        assert rec.run_id.startswith("sp1.echo.")
        assert rec.status in ("running", "succeeded")  # RUNNING at spawn; may finish fast
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            got = svc.get_run(rec.run_id)
            if got and got.status != "running":
                break
            time.sleep(0.05)
        got = svc.get_run(rec.run_id)
        assert got is not None and got.status == "succeeded"
    finally:
        svc.shutdown()


def test_run_many_unknown_client_fails_fast(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError):
        svc.run_many([{"client": "nope", "goal": "x"}])
    assert svc.status() == []  # nothing ran - validated before launching


def test_integrate_empty_run_is_noop(repo: Path) -> None:
    svc = _svc(repo)  # _Echo prints but writes no files
    rec = svc.run_agent("worker", "do nothing", task_id="e1")
    result = svc.integrate(rec.run_id)
    assert result.status == "empty"


def test_doctor_reports_checks_and_serializes(repo: Path) -> None:
    svc = _svc(repo)  # in-memory config; no fleet.config.yaml on disk
    report = svc.doctor()
    by_name = {c.name: c for c in report.checks}
    assert {"python", "git", "repo"} <= set(by_name)
    assert by_name["repo"].status == "ok"  # the fixture is a real git work tree
    assert by_name["config"].status == "fail"  # no config file on disk -> a failing check
    assert report.ok is (report.fails == 0) and report.ok is False
    assert report.model_dump(mode="json")["fails"] >= 1  # fully serializable for the MCP surface


def test_doctor_probes_configured_backends(repo: Path) -> None:
    cfg_file = repo / "fleet.config.yaml"
    cfg_file.write_text("clients:\n  worker:\n    backend: echo\n    permission: safe-edit\n")
    svc = MarshalService(
        repo, load_config(cfg_file), backends={"echo": _Echo()}, config_path=cfg_file
    )
    by_name = {c.name: c for c in svc.doctor().checks}
    assert by_name["config"].status == "ok"
    assert by_name["backend:echo"].status == "ok"  # _Echo.check_available() is True


def _mixed_svc(repo: Path) -> MarshalService:
    """A service with one available ('echo') and one unavailable ('missing') client."""
    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(name="worker", backend="echo", permission=PermissionMode.SAFE_EDIT),
            "ghost": ClientConfig(name="ghost", backend="missing", permission=PermissionMode.SAFE_EDIT),
        }
    )
    return MarshalService(repo, cfg, backends={"echo": _Echo(), "missing": _Missing()})


def test_unavailable_client_skipped(repo: Path) -> None:
    svc = _mixed_svc(repo)
    # (a) the unavailable client is absent from list_clients, present in skipped_clients
    listed = {c.name for c in svc.list_clients().clients}
    assert "ghost" not in listed
    assert "worker" in listed
    assert svc.skipped_clients == ["ghost"]


def test_run_agent_on_skipped_client_raises(repo: Path) -> None:
    svc = _mixed_svc(repo)
    # (b) run_agent on a skipped client raises ValueError (it is no longer in self._clients)
    with pytest.raises(ValueError):
        svc.run_agent("ghost", "do something", task_id="t1")
    assert svc.status() == []  # nothing ran


def test_all_available_skipped_is_empty(repo: Path) -> None:
    # (c) a service with only available backends has skipped_clients == []
    svc = _svc(repo)  # single 'echo' client, _Echo.check_available() is True
    assert svc.skipped_clients == []
    assert {c.name for c in svc.list_clients().clients} == {"worker"}


# --- harness-first model selection: model override + ad-hoc (backend, model) spawn ------------


def _opencode_svc(repo: Path) -> MarshalService:
    """A service whose configured client uses the opencode backend (the Fireworks guard is opencode-specific)."""
    cfg = FleetConfig(
        clients={
            "impl": ClientConfig(
                name="impl", backend="opencode", model="opencode-go/anything",
                permission=PermissionMode.SAFE_EDIT,
            )
        }
    )
    # Inject a no-op opencode backend so the service doesn't try to call the real `opencode` CLI
    # in CI; the synthesis path only consults make_backend() for ad-hoc, but the configured client
    # is the one that exercises the Fireworks guard via the override channel.
    from marshal_engine.backends.opencode import OpenCodeBackend

    fake = OpenCodeBackend()
    fake.check_available = lambda: True  # type: ignore[method-assign]
    return MarshalService(repo, cfg, backends={"opencode": fake})


def test_request_for_adhoc_synthesizes_ephemeral_config(repo: Path) -> None:
    # Ad-hoc: backend=echo (already on the fleet via _Echo), no client. The synthesized request
    # uses fleet-default permission + timeout, the caller's model, and an `adhoc-<backend>` client
    # name. resolve_model still applies its opencode default for ad-hoc opencode without a model.
    svc = _svc(repo)
    req = svc._request_for(None, "x", backend="echo", model="custom-model")
    assert req.backend_name == "echo"
    assert req.model == "custom-model"
    assert req.client == "adhoc-echo"
    assert req.permission == PermissionMode.SAFE_EDIT  # fleet default
    assert req.timeout_s == 600  # fleet default

    # Ad-hoc opencode without an explicit model: resolve_model defaults to the Go subscription.
    req2 = svc._request_for(None, "x", backend="opencode")
    assert req2.backend_name == "opencode"
    assert req2.model == DEFAULT_OPENCODE_MODEL


def test_request_for_both_client_and_backend_prefers_client(repo: Path) -> None:
    # When both `client_name` and `backend` are given, client wins (backend is ignored). The
    # client's resolved backend/model are used; `model` still overrides the resolved model.
    svc = _svc(repo)
    # The service's "worker" client is backend="echo" with no model. Ad-hoc would be "opencode".
    # Passing both -> the run uses "echo" (client's), not "opencode".
    req = svc._request_for("worker", "x", backend="opencode")
    assert req.backend_name == "echo"
    assert req.client == "worker"
    # And the explicit model still overrides whatever the client resolves to.
    req2 = svc._request_for("worker", "x", backend="opencode", model="explicit")
    assert req2.backend_name == "echo"
    assert req2.model == "explicit"


def test_request_for_neither_client_nor_backend_raises(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError, match="client.*backend|backend.*client"):
        svc._request_for(None, "x")


def test_request_for_unknown_backend_raises_with_valid_names(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError) as exc:
        svc._request_for(None, "x", backend="nonexistent")
    # The registry's own message lists the valid backends; the test asserts the names are surfaced.
    msg = str(exc.value)
    assert "nonexistent" in msg
    assert "known" in msg
    # Each registered backend name appears in the error so the driver can fix the typo.
    from marshal_engine.registry import backend_names

    for name in backend_names():
        assert name in msg


def test_request_for_adhoc_opencode_fireworks_model_raises(repo: Path) -> None:
    # The Fireworks guard applies to ad-hoc opencode configs the same way it does to configured
    # ones - synthesized at request-time, so a typo'd model fails fast before any spawn.
    svc = _svc(repo)
    with pytest.raises(ConfigError, match="Fireworks"):
        svc._request_for(None, "x", backend="opencode", model="fireworks-ai/accounts/fireworks/models/glm-5p2")


def test_run_agent_model_override_on_configured_client(repo: Path) -> None:
    # end-to-end: a configured client with model "configured-model", then call with model="override";
    # the override reaches the RunRecord (which is what get_run / status / usage / report see).
    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(
                name="worker", backend="echo", model="configured-model",
                permission=PermissionMode.SAFE_EDIT,
            )
        }
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    rec = svc.run_agent("worker", "do x", task_id="t1", model="override")
    assert rec.status == "succeeded"
    assert rec.model == "override"  # override reaches the persisted record

    # And without the override, the client's resolved model is used.
    rec2 = svc.run_agent("worker", "do x", task_id="t2")
    assert rec2.model == "configured-model"  # resolve_model(client)


def test_run_agent_adhoc_backend_runs_without_configured_client(repo: Path) -> None:
    # A service with NO clients (e.g. an empty config) can still spawn by bare (backend, model).
    cfg = FleetConfig()  # no clients
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    rec = svc.run_agent(backend="echo", goal="do x", task_id="t1", model="adhoc-model")
    assert rec.status == "succeeded"
    assert rec.backend == "echo"
    assert rec.model == "adhoc-model"
    assert rec.client == "adhoc-echo"


def test_run_agent_unknown_backend_raises(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError, match="nonexistent"):
        svc.run_agent(backend="nonexistent", goal="x", task_id="t1")


def test_run_agent_opencode_fireworks_model_raises_config_error(repo: Path) -> None:
    # Ad-hoc path: run_agent propagates the same ConfigError the synthesis raises, so a
    # Fireworks-billed run never starts on the Fleet.
    svc = _opencode_svc(repo)
    with pytest.raises(ConfigError, match="Fireworks"):
        svc.run_agent(backend="opencode", goal="x", task_id="t1",
                      model="fireworks-ai/accounts/fireworks/models/glm-5p2")


def test_run_agent_client_model_override_fireworks_raises(repo: Path) -> None:
    # Override path: a model override on a CONFIGURED opencode client must hit the same Fireworks
    # guard as an ad-hoc opencode run. Overrides bypass load_config, so _request_for re-checks.
    svc = _opencode_svc(repo)  # configured client "impl" is backend=opencode
    with pytest.raises(ConfigError, match="Fireworks"):
        svc.run_agent("impl", "x", task_id="t1",
                      model="fireworks-ai/accounts/fireworks/models/glm-5p2")
