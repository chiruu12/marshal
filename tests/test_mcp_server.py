"""Tests for the MCP server wiring (build_service + tool registration)."""

from __future__ import annotations

import inspect
import urllib.parse
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

import marshal_engine.interfaces.mcp_server.server as server_mod
from marshal_engine.core.config import ConfigError
from marshal_engine.interfaces.mcp_server import build_service
from marshal_engine.interfaces.mcp_server.schema import Job, ThenJob
from marshal_engine.orchestration.workflow import WorkflowRunner

_CONFIG = """
clients:
  reviewer:
    backend: cursor
    permission: read-only
"""


def _repo_with_config(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fleet.config.yaml").write_text(_CONFIG)
    return repo


def test_build_service_from_env_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    # Assert on the PARSED config, not list_clients(): the latter filters out clients whose backend
    # CLI isn't installed (graceful skip), so a cursor-backed client vanishes on a clean CI runner
    # that has no cursor-agent. This test's job is "build_service loaded the env-pointed config".
    assert "reviewer" in svc.config.clients


def test_build_service_without_config_starts_with_zero_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A freshly installed plugin has no fleet.config.yaml; the server must still start (not crash)
    # so the driver can connect and be told to configure a fleet.
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    assert svc.list_clients().clients == []
    assert "no fleet config" in capsys.readouterr().err


def test_list_workflows_surfaces_malformed_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    wdir = repo / "workflows"
    wdir.mkdir()
    (wdir / "broken.yaml").write_text("name: broken\nphases: not-a-list\n")
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    structured = asyncio.run(app.call_tool("list_workflows", {})).structured_content
    payload = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert payload["workflows"] == []
    assert "broken.yaml" in payload["errors"]
    assert "invalid" in payload["errors"]["broken.yaml"].lower()


def test_run_workflow_missing_yaml_path_is_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marshal_engine.core.config import ConfigError

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    # an explicit .yaml path that doesn't exist must NOT be re-treated as a bare name (which would
    # look for "<dir>/x.yaml.yaml" and raise a misleading "no workflow 'x.yaml'").
    with pytest.raises(ConfigError, match="no workflow file at"):
        svc.run_workflow("does-not-exist.yaml")


@pytest.mark.parametrize("form", ["review.yaml", "workflows/review.yaml"])
def test_run_workflow_accepts_both_documented_relative_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    # A relative path may be written against the workflows dir OR the repo root; both are
    # documented. Resolving only against the workflows dir doubles the prefix for the second form.
    repo = _repo_with_config(tmp_path)
    wf = repo / "workflows"
    wf.mkdir(exist_ok=True)
    (wf / "review.yaml").write_text(
        "phases:\n  - run: fan_out\n    clients: [a]\n    goal: g\n", encoding="utf-8"
    )
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    # Stub the runner so this test is purely about path resolution. Capture what it received:
    # an exception-swallowing assertion would pass even when resolution is broken.
    seen: list[object] = []
    monkeypatch.setattr(
        WorkflowRunner, "run",
        lambda self, spec, inputs, max_concurrency=4: (seen.append(spec), "ran")[1],
    )
    assert svc.run_workflow(form) == "ran"
    assert len(seen) == 1  # resolution reached the runner for both documented forms


@pytest.mark.parametrize("bad", ["../../evil.yaml", "/tmp/evil.yaml"])
def test_run_workflow_refuses_paths_outside_the_workflows_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    with pytest.raises(ConfigError, match="outside"):
        svc.run_workflow(bad)


def test_build_app_registers_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    names = {t.name for t in asyncio.run(app.list_tools())}
    expected = {
        "set_outcome", "routing",
        "run_agent", "run_many", "spawn", "benchmark", "report", "list_clients", "list_models",
        "status", "usage", "get_run", "get_run_log", "collect_run", "commit_run", "integrate", "clean",
        "cancel_run", "wait_for_runs", "list_workflows", "run_workflow", "doctor", "list_teams",
        "run_team",
    }
    assert expected <= names


def test_list_teams_surfaces_malformed_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    tdir = repo / "teams"
    tdir.mkdir()
    (tdir / "broken.yaml").write_text("roles: not-a-list\n")
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    structured = asyncio.run(app.call_tool("list_teams", {})).structured_content
    payload = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert payload["teams"] == []
    assert "broken.yaml" in payload["errors"]


def test_run_team_maps_its_flat_params_onto_the_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP tool flattens six params into a TeamSubject; a swapped base/head would ship silently."""
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    seen: dict[str, object] = {}

    def fake_run_team(name: str, subject: object, **kw: object) -> object:
        seen["name"] = name
        seen["subject"] = subject
        raise ConfigError("stop here - the mapping is what is under test")

    monkeypatch.setattr(svc, "run_team", fake_run_team)
    app = build_app(svc)
    with pytest.raises(Exception, match="the mapping is what is under test"):
        asyncio.run(
            app.call_tool(
                "run_team",
                {
                    "name": "gate", "target": "range", "base": "main", "head": "feature",
                    "paths": ["src/", "tests/"],
                },
            )
        )
    subject = seen["subject"]
    assert seen["name"] == "gate"
    assert subject.kind == "range"  # type: ignore[attr-defined]
    assert subject.base == "main"  # type: ignore[attr-defined]
    assert subject.head == "feature"  # type: ignore[attr-defined]
    # Every path must land: dropping `paths` here would silently review the WHOLE diff.
    assert subject.paths == ["src/", "tests/"]  # type: ignore[attr-defined]
    assert subject.run_id is None and subject.text is None  # type: ignore[attr-defined]

    seen.clear()
    with pytest.raises(Exception, match="the mapping is what is under test"):
        asyncio.run(
            app.call_tool(
                "run_team", {"name": "gate", "target": "run", "run_id": "r7"}
            )
        )
    assert seen["subject"].run_id == "r7"  # type: ignore[attr-defined,union-attr]
    assert seen["subject"].paths == []  # type: ignore[attr-defined,union-attr]

    seen.clear()
    with pytest.raises(Exception, match="the mapping is what is under test"):
        asyncio.run(
            app.call_tool("run_team", {"name": "gate", "target": "plan", "text": "a plan"})
        )
    assert seen["subject"].text == "a plan"  # type: ignore[attr-defined,union-attr]


def test_tools_are_async_and_round_trip_via_call_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tools are async and offload to a worker thread; calling one must execute end-to-end (this
    # would deadlock/raise if the async+offload wiring were wrong).
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    structured = asyncio.run(app.call_tool("list_clients", {})).structured_content
    payload = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert isinstance(payload, dict)
    assert "clients" in payload
    # When cursor-agent is present locally the reviewer client appears with fidelity;
    # when absent, clients is empty (graceful skip). Either way the field shape is stable.
    for client in payload["clients"]:
        assert "permission_fidelity" in client
        assert client["permission_fidelity"] in {
            "enforced-denies",
            "boundary-only",
            "unrestricted",
        }


def test_list_models_round_trips_via_call_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The catalog tool mirrors list_clients: it must round-trip through call_tool and return
    # cleanly (empty models on a config with no `models:` block, but the tool must exist).
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    result = asyncio.run(app.call_tool("list_models", {}))
    assert result is not None


def test_status_is_compact_and_reports_what_it_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One observed `status` reply was ~395k characters - mostly agent prose the caller had not
    asked for - so its only consumer (a context-bounded agent) stopped calling it. It is compact by
    default and pages, and it must never cap silently: a driver that reads a truncated list as the
    whole ledger draws exactly the wrong conclusion."""
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app
    from marshal_engine.runtime.state import FleetState, RunRecord

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    runs = FleetState(repo / ".marshal" / "runs")
    for i in range(5):
        runs.add(
            RunRecord(
                run_id=f"r{i}.echo.x",
                task_id="t" if i < 3 else "other",
                backend="echo",
                status="exited_clean",
                started_at=f"2026-01-0{i + 1}T00:00:00+00:00",
                text="x" * 5000,  # the bulk this view exists to drop
            )
        )
    app = build_app(build_service())

    def call(**kw: object) -> dict[str, Any]:
        payload = asyncio.run(app.call_tool("status", kw)).structured_content
        return payload  # type: ignore[return-value]

    out = call(limit=2)
    assert out["returned"] == 2 and out["matched"] == 5
    assert out["truncated"] is True, "a capped list must say so, never look complete"
    assert out["runs"][0]["run_id"] == "r4.echo.x", "newest first"
    assert "text" not in out["runs"][0], "the default view still carried the bulky field"
    assert out["runs"][0]["has_text"] is True, "cannot tell an omitted field from an empty one"
    # The default is the POLL shape: enough to decide "done? any good?" and nothing else. Polling
    # is the highest-frequency call a driver makes, so every extra field is paid for on every poll.
    assert set(out["runs"][0]) == {
        "run_id", "task_id", "backend", "client", "status", "agent_alive",
        "cost_usd", "source", "duration_ms", "outcome", "ended_at",
        "has_text", "has_verify_output",
        "workspace",  # added by the multi-workspace tag, not part of the record
    }
    assert out["view"] == "poll"

    assert call(task_id="t")["matched"] == 3
    assert call(status="failed")["matched"] == 0
    # `since_hours` is its own branch and was briefly shipped with a NameError in it: the whole
    # suite stayed green because nothing exercised the filter. Cover it.
    assert call(since_hours=1.0)["matched"] == 0, "all five runs are dated 2026-01, well outside 1h"
    assert call(since_hours=24 * 365 * 100)["matched"] == 5
    # Widening is opt-in, and each step up is a superset of the last.
    compact = call(limit=1, view="compact")["runs"][0]
    assert "text" not in compact and compact["has_text"] is True
    assert "worktree" in compact and "input_tokens" in compact, "compact must keep the details"
    assert set(call(limit=1)["runs"][0]) < set(compact), "poll must be a strict subset of compact"

    full = call(limit=1, view="full")["runs"][0]
    assert full["text"] == "x" * 5000
    assert "has_text" not in full, "the flag is a stand-in for the field, not a companion to it"


def test_duration_param_is_wired_into_spawn_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The per-spawn `duration` override must be exposed on the tool schema so a driver can pass a
    # preset; assert the schema (not calling spawn, which would start a real run).
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    assert "duration" in tools["spawn"].input_schema["properties"]
    assert "duration" in tools["run_agent"].input_schema["properties"]


def test_base_branch_param_is_wired_into_spawn_and_run_agent_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    assert "base_branch" in tools["spawn"].input_schema["properties"]
    assert "base_branch" in tools["run_agent"].input_schema["properties"]


def test_spawn_base_branch_reaches_task_spec_via_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio
    import subprocess
    import sys
    import time

    from marshal_engine.backends.base import CodingAgentBackend
    from marshal_engine.core.config import ClientConfig, FleetConfig, PermissionMode
    from marshal_engine.core.types import AgentResult, Capabilities, RunOpts, RunStatus, TaskSpec
    from marshal_engine.interfaces.mcp_server import build_app
    from marshal_engine.interfaces.service import MarshalService

    class _Capture(CodingAgentBackend):
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
            return AgentResult(status=RunStatus.EXITED_CLEAN, text=raw_stdout.strip(), exit_code=exit_code)

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "README.md").write_text("hi")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("branch", "marshal/prior", cwd=repo)
    (repo / "fleet.config.yaml").write_text(
        "clients:\n  worker:\n    backend: capture\n    permission: safe-edit\n"
    )

    backend = _Capture()
    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="capture", permission=PermissionMode.SAFE_EDIT)}
    )
    svc = MarshalService(repo, cfg, backends={"capture": backend})
    app = build_app(svc)
    try:
        asyncio.run(
            app.call_tool(
                "spawn",
                {
                    "client": "worker",
                    "goal": "do x",
                    "task_id": "mcp1",
                    "base_branch": "marshal/prior",
                },
            )
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not backend.tasks:
            time.sleep(0.05)
        assert backend.tasks[-1].base_branch == "marshal/prior"
    finally:
        svc.shutdown()


def test_tool_params_carry_schema_descriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Self-describing params: the driver should see a description per parameter, not just type+title.
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    props = tools["run_agent"].input_schema["properties"]
    assert props["client"].get("description")
    assert props["context_files"].get("description")
    assert props["read_paths"].get("description")
    spawn_props = tools["spawn"].input_schema["properties"]
    assert spawn_props["read_paths"].get("description")


def test_run_handle_tools_refuse_a_traversal_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # REGRESSION: a `../../<ws>/.marshal/runs/<id>` run_id stat'ed through one workspace's ledger
    # into ANOTHER workspace's run record (cross-tenant read), and resolved any host `*.json` as
    # an existence oracle. Every run-handle tool now gets a clean validation error (refused at
    # the registry boundary), never a resolved foreign record or a raw traceback.
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    traversal = "../../ws_b/.marshal/runs/abc"
    # get_run/get_run_log go through resolve_run; the rest through require_run - same refusal.
    # The MCP SDK wraps a tool exception as ToolError("Error executing tool <name>: <cause>"),
    # so the driver sees the refusal reason, not a traceback.
    for tool, args in (
        ("get_run", {"run_id": traversal}),
        ("get_run_log", {"run_id": traversal}),
        ("collect_run", {"run_id": traversal}),
        ("cancel_run", {"run_id": traversal}),
        ("commit_run", {"run_id": traversal}),
        ("read_run_file", {"run_id": traversal, "path": "x"}),
        ("integrate", {"run_id": traversal}),
    ):
        with pytest.raises(Exception, match="unsafe run_id"):
            asyncio.run(app.call_tool(tool, args))


def test_get_run_log_round_trips_via_call_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # get_run_log is the per-run durable-log equivalent of get_run: it locates the run by id
    # across workspaces, reads <base>/logs/<run_id>.log, and returns it stamped with the workspace.
    # `log` is the stored text when present and null when the run is known but no log was written.
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app
    from marshal_engine.runtime.logs import RunLogStore

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()

    # Stamp a synthetic log under the Fleet's logs dir (built off the service's repo_root).
    logs_dir = repo / ".marshal" / "logs"
    RunLogStore(logs_dir).write("synthetic.run", "the-stdout\n", "the-stderr\n")
    # And: the run must exist in state too, so resolve_run() can find it (otherwise the tool
    # short-circuits to log=null without ever consulting RunLogStore).
    from marshal_engine.runtime.state import RunRecord
    svc.fleet.state.add(
        RunRecord(run_id="synthetic.run", task_id="synthetic", backend="cursor", status="exited_clean")
    )

    app = build_app(svc)
    structured = asyncio.run(app.call_tool("get_run_log", {"run_id": "synthetic.run"})).structured_content
    out = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert out["run_id"] == "synthetic.run"
    assert "the-stdout" in out["log"]
    assert "the-stderr" in out["log"]
    assert "=== run synthetic.run ===" in out["log"]
    assert out["workspace"] == "default"  # tag() stamps the owning workspace

    # And: a run id no workspace owns is an ERROR, not a null log. Returning `{log: null}` made
    # "no such run" byte-identical to "this run wrote no log", so a driver read a typo'd or
    # already-cleaned id as a run that crashed before producing diagnostics, and re-spawned it.
    with pytest.raises(Exception, match="no run 'nope.run'"):
        asyncio.run(app.call_tool("get_run_log", {"run_id": "nope.run"}))


def test_get_run_log_does_not_stamp_a_workspace_it_never_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old null-log answer echoed the caller's `workspace` back, naming a workspace nothing
    had resolved - and one that need not be registered at all. `status`, `doctor` and
    `list_clients` all reject an unregistered name; this quietly agreed with it."""
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())

    with pytest.raises(Exception, match="no run 'ghost.run'"):
        asyncio.run(
            app.call_tool("get_run_log", {"run_id": "ghost.run", "workspace": "not-registered"})
        )


def test_usage_window_param_is_in_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The `window` parameter must be on the tool schema with the canonical USAGE_WINDOWS set.
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.accounting.usage import USAGE_WINDOWS
    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    props = tools["usage"].input_schema["properties"]
    assert "window" in props
    assert set(props["window"]["enum"]) == set(USAGE_WINDOWS)


def test_usage_window_param_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Drive the tool end-to-end: each `window` value resolves to the expected `since`. Recording a
    # 2020 event lets us see session/week/month all filter it out (it's outside every window) and
    # the unfiltered "all" keep it. The 2026 event lands in every window.
    import asyncio
    from datetime import datetime

    from marshal_engine.accounting.usage import UsageEvent, UsageTracker

    pytest.importorskip("mcp")
    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    app = build_app(svc)

    # Stamp one event in the (far) past, one firmly inside every rolling window. Pin session_start
    # before the "new" event so the session filter is deterministic (not wall-clock-relative).
    from datetime import timedelta

    now = datetime.now(UTC)
    session_start = now - timedelta(hours=1)
    new_ts = (now - timedelta(minutes=1)).isoformat()
    svc.fleet.session_start = session_start
    u = tmp_path / "ledger" / "usage"
    u.mkdir(parents=True)
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts="2020-01-01T00:00:00Z", run_id="old", backend="opencode", cost_usd=1.00,
        ).model_dump_json() + "\n"
        + UsageEvent(
            ts=new_ts, run_id="new", backend="opencode", cost_usd=0.01,
        ).model_dump_json() + "\n"
    )
    # Point the service's UsageTracker at our test ledger (replacing the default empty one).
    svc.fleet.usage = UsageTracker(u)

    def _call(window: str) -> dict:
        structured = asyncio.run(app.call_tool("usage", {"window": window})).structured_content
        if isinstance(structured, dict):
            return structured.get("result", structured)
        return structured  # type: ignore[return-value]

    # all = no filter: both events present
    out_all = _call("all")
    assert out_all["window"] == "all"
    assert out_all["since"] is None
    assert out_all["totals"]["runs"] == 2
    assert abs(out_all["totals"]["cost_usd"] - 1.01) < 1e-9

    # week/month = now-Nd, the 2020 event is excluded
    out_week = _call("week")
    assert out_week["window"] == "week"
    assert out_week["since"] is not None
    assert out_week["totals"]["runs"] == 1
    assert abs(out_week["totals"]["cost_usd"] - 0.01) < 1e-9

    out_month = _call("month")
    assert out_month["window"] == "month"
    assert out_month["totals"]["runs"] == 1

    # day = last 24h; the 2020 event is excluded
    out_day = _call("day")
    assert out_day["window"] == "day"
    assert out_day["since"] is not None
    assert out_day["totals"]["runs"] == 1

    # session = since = svc.session_start; the "new" event is after that pinned start.
    out_session = _call("session")
    assert out_session["window"] == "session"
    since = datetime.fromisoformat(out_session["since"])
    assert since == session_start
    assert out_session["totals"]["runs"] == 1
    assert abs(out_session["totals"]["cost_usd"] - 0.01) < 1e-9
    # Windowed JSON includes the new by_backend_model key
    assert "by_backend_model" in out_session


def test_quickstart_names_the_loop_and_disambiguates_the_lookalike_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A driver seeing ~20 tools had no stated ordering and no decision boundary between the four
    run-ish and four status-ish tools - it learned "spawn is the long one" only by reading each
    description. This tool is where that orientation lives, because a driver reads tool
    descriptions, not the README."""
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())

    names = {t.name for t in asyncio.run(app.list_tools())}
    assert "marshal_quickstart" in names

    payload = asyncio.run(app.call_tool("marshal_quickstart", {})).structured_content
    # The four-step spine, in order.
    steps = " ".join(payload["the_loop"])
    for tool in ("doctor", "spawn", "collect_run", "integrate"):
        assert tool in steps, f"the canonical loop does not mention {tool}"
    # Every lookalike is disambiguated, which is the actual complaint.
    assert {"run_agent", "spawn", "run_many", "run_workflow"} <= set(payload["which_run_tool"])
    assert {"status", "get_run", "collect_run", "get_run_log"} <= set(payload["which_read_tool"])
    # The blocking-vs-async distinction is stated, not left to be inferred from the names.
    assert "Blocks" in payload["which_run_tool"]["run_agent"]
    assert "does not hold your turn" in payload["which_run_tool"]["spawn"]
    # And the caveat that every reviewer flagged is stated up front, not buried.
    assert "not a claim about" in payload["safety"]
    # A driver asked for a 12-agent research fan-out read this framing and went elsewhere, because
    # the description implied code-only through collect_run/integrate/worktrees. It must say what
    # Marshal is, name read-and-reason uses, and treat DIFF/TEXT as first-class.
    assert "fleet primitive" in payload["what_marshal_is"]
    assert "not the only one" in payload["what_marshal_is"]
    assert "DIFF or TEXT" in payload["what_marshal_is"] or "diff or text" in payload["what_marshal_is"].lower()
    for use in ("research", "review", "audit", "summarise"):
        assert use in payload["what_marshal_is"].lower(), f"quickstart omits use case {use!r}"
    # An earlier draft claimed a read-and-reason run "ends `empty`". It does not: `text` alone is
    # enough for exited_clean (see `_authoritative_status`), and the message is on the record. Saying
    # otherwise pushed drivers to write files they did not need to. Assert the corrected claim.
    assert "get_run" in payload["non_code_runs"]
    assert "collect_run" in payload["non_code_runs"], "must name collect_run's `produced` field"
    assert "outcome" in payload["non_code_runs"].lower()
    assert "exited_clean" in payload["safety"]


def test_no_marshal_surface_claims_a_text_run_ends_empty() -> None:
    """The wrong claim was fixed in the quickstart but survived in the module docstring - a partial
    correction is how a false statement outlives its own retraction. `_authoritative_status` returns
    SUCCEEDED on text alone, so nothing may say otherwise anywhere a driver or user reads."""
    import marshal_engine.interfaces.mcp_server as srv

    # EVERY surface, not a sample. Scoping this to three files is how the claim survived a second
    # round: it was corrected in the quickstart, then the module docstring, and was still sitting in
    # the CHANGELOG. A partial sweep of a false statement is how it outlives its own retraction.
    surfaces = {"mcp_server docstring": srv.__doc__ or ""}
    for name in ("README.md", "CHANGELOG.md", "CLAUDE.md", "SECURITY.md", "CONTRIBUTING.md"):
        surfaces[name] = Path(name).read_text(encoding="utf-8")
    for doc in sorted(Path("docs").rglob("*.md")):
        if "internal" in doc.parts:
            continue  # local-only notes; they record the history of the mistake on purpose
        surfaces[str(doc)] = doc.read_text(encoding="utf-8")
    for where, text in surfaces.items():
        lowered = text.lower()
        assert "ends `empty`" not in lowered, f"{where} still claims a text run ends empty"
        assert "produces an `empty` diff" not in lowered, f"{where} still claims an empty diff"


def test_quickstart_claims_are_true_of_the_actual_tool_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orientation tool that overclaims is worse than none - it is the same defect as
    `succeeded` and `configured`, just in prose. Two earlier drafts said "integrate is the ONLY
    step that touches your branch" (a workflow with an `auto: true` integrate phase also does) and
    "every tool takes an optional workspace" (the global tools do not). Pin both against the real
    registered signatures rather than against what the text asserts."""
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    payload = asyncio.run(app.call_tool("marshal_quickstart", {})).structured_content

    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    takes_workspace = {
        name for name, t in tools.items()
        if "workspace" in (t.input_schema or {}).get("properties", {})
    }
    globals_ = {"marshal_quickstart", "list_workspaces", "add_workspace"}
    assert not (globals_ & takes_workspace), "a global tool grew a workspace param"
    assert takes_workspace, "no tool takes workspace - the claim would be vacuous"
    # So the text must NOT say "every tool".
    assert "Every tool takes" not in payload["multi_repo"]

    # `run_workflow` can integrate, so integrate is not the only path to the user's branch.
    assert "run_workflow" in tools
    assert "only step that touches" not in " ".join(payload["the_loop"])
    assert "run_workflow" in payload["safety"]


def test_server_reports_its_version_in_the_handshake(tmp_path: Path) -> None:
    """An empty serverInfo.version makes "which Marshal is this?" unanswerable from the client.

    That is the first question you ask when a tool misbehaves, so the handshake must answer it.
    """
    import marshal_engine
    from marshal_engine.core.config import ClientConfig, FleetConfig, PermissionMode
    from marshal_engine.interfaces.mcp_server import build_app
    from marshal_engine.interfaces.service import MarshalService

    cfg = FleetConfig(
        clients={"c": ClientConfig(name="c", backend="cursor", permission=PermissionMode.SAFE_EDIT)}
    )
    app = build_app(MarshalService(tmp_path, cfg, backends={}))
    assert app.version == marshal_engine.__version__
    assert app.version, "serverInfo.version must not be empty"


def test_spawn_tool_docstring_promises_return_before_provisioning() -> None:
    """#146: spawn's MCP docstring must not overclaim — return is before setup_cmd/provisioning."""
    from marshal_engine.interfaces.mcp_server import tools_runs

    src = Path(tools_runs.__file__).read_text(encoding="utf-8")
    assert "before worktree provisioning" in src
    assert "cancel_run stops an in-flight setup" in src


def test_output_schema_param_is_wired_into_spawn_and_run_agent_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    assert "output_schema" in tools["spawn"].input_schema["properties"]
    assert "output_schema" in tools["run_agent"].input_schema["properties"]
    job_props = tools["run_many"].input_schema["$defs"]["Job"]["properties"]
    assert "output_schema" in job_props


def test_run_agent_rejects_invalid_output_schema_via_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad schema dict → clean tool error (invalid output_schema), not a traceback."""
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    with pytest.raises(Exception, match="invalid output_schema"):
        asyncio.run(
            app.call_tool(
                "run_agent",
                {
                    "goal": "x",
                    "backend": "opencode",
                    "output_schema": {"type": "object", "properties": "not-an-object"},
                },
            )
        )


def test_set_outcome_round_trips_and_returns_conflict_as_a_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused overwrite must come back as data, not an exception - drivers branch on it."""
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app
    from marshal_engine.runtime.state import FleetState, RunRecord

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    state = FleetState(repo / ".marshal" / "runs")
    state.add(RunRecord(run_id="r1", task_id="t1", backend="echo", status="exited_clean"))

    app = build_app(build_service())
    assert asyncio.run(app.call_tool("set_outcome", {"run_id": "r1", "outcome": "rejected"}))

    state.update("r1", outcome="integrated")
    assert asyncio.run(app.call_tool("set_outcome", {"run_id": "r1", "outcome": "rejected"}))
    rec = state.get("r1")
    assert rec is not None and rec.outcome == "integrated"  # the sticky verdict survived


def test_artifacts_from_is_wired_into_the_run_tools_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver reaches Marshal through MCP, so a param the engine accepts but the tool schema
    omits does not exist as far as the only user is concerned."""
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    for name in ("spawn", "run_agent"):
        assert "artifacts_from" in tools[name].input_schema["properties"], name
    job = tools["run_many"].input_schema["$defs"]["Job"]["properties"]
    assert "artifacts_from" in job, "a run_many job cannot pass a report to its round-2 sibling"


def test_quickstart_only_names_parameters_that_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `tool(param=...)` the quickstart recommends must be a real parameter of that tool.

    `marshal_quickstart` is the driver's map of the server, so a parameter renamed out from under
    it sends drivers at an argument that no longer exists - and an unknown kwarg over MCP is
    dropped silently, so the driver gets the default view while believing it asked for something
    else. Caught in review once (`status(full=true)` outliving the `full` bool); this closes it.
    """
    pytest.importorskip("mcp")
    import asyncio
    import json
    import re

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}

    text = json.dumps(asyncio.run(app.call_tool("marshal_quickstart", {})).structured_content)
    referenced = set(re.findall(r"(\w+)\((\w+)=", text))
    assert referenced, "the quickstart stopped naming any tool parameters - regex likely stale"
    for tool_name, param in sorted(referenced):
        if tool_name not in tools:
            continue  # prose like `list(x=...)`, not a tool reference
        assert param in tools[tool_name].input_schema["properties"], (
            f"quickstart recommends {tool_name}({param}=...) but {tool_name} has no {param!r}"
        )


def test_quickstart_routes_every_run_reading_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`which_read_tool` must name every tool that reads a run, and only tools that exist.

    The names - `get_`, `collect_`, `read_` - are three verbs for one operation, so none of them
    says which returns the record, the log, the diff, or a file. This map is the only thing that does, so a read tool
    added without a row here is a tool a driver has to discover by trial. Enumerated explicitly
    rather than inferred, because "reads a run" is a judgement about intent that no naming pattern
    can be trusted to detect - adding the sixth read tool should force a decision here.
    """
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name for t in asyncio.run(app.list_tools())}

    routed = asyncio.run(app.call_tool("marshal_quickstart", {})).structured_content
    assert routed is not None
    mapped = set(routed["which_read_tool"])

    assert mapped == {"status", "get_run", "collect_run", "get_run_log", "read_run_file"}
    assert mapped <= tools, f"which_read_tool routes to tools that do not exist: {mapped - tools}"


def test_wait_for_runs_round_trips_and_tags_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settled records must come back workspace-tagged: a wait may span repos, and an untagged
    record is not addressable by the integrate/collect calls that follow it."""
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app
    from marshal_engine.runtime.state import FleetState, RunRecord

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    state = FleetState(repo / ".marshal" / "runs")
    state.add(RunRecord(run_id="r-done", task_id="t", backend="echo", status="exited_clean"))

    app = build_app(build_service())
    payload = asyncio.run(
        app.call_tool("wait_for_runs", {"run_ids": ["r-done", "r-ghost"], "timeout_s": 1})
    ).structured_content

    assert payload is not None
    assert payload["timed_out"] is False
    assert [r["run_id"] for r in payload["settled"]] == ["r-done"]
    assert payload["settled"][0]["workspace"]
    assert payload["unknown"] == ["r-ghost"]
    assert payload["pending"] == []


def test_wait_for_runs_reports_a_malformed_id_as_unknown_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad id must not throw away the runs that already finished beside it.

    Run-id validation is fail-closed (a `../` id would compose a path into another workspace's
    ledger) and still refuses here - no path is built from it. But this is a batch call whose
    contract is that every id lands in exactly one list, so the refusal is reported as `unknown`
    rather than raised, the same partial-over-nothing rule the timeout path follows.
    """
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app
    from marshal_engine.runtime.state import FleetState, RunRecord

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    FleetState(repo / ".marshal" / "runs").add(
        RunRecord(run_id="r-good", task_id="t", backend="echo", status="exited_clean")
    )

    app = build_app(build_service())
    payload = asyncio.run(
        app.call_tool(
            "wait_for_runs",
            {"run_ids": ["r-good", "../../etc/passwd"], "timeout_s": 1},
        )
    ).structured_content

    assert payload is not None
    assert [r["run_id"] for r in payload["settled"]] == ["r-good"]
    assert payload["unknown"] == ["../../etc/passwd"]
    assert payload["timed_out"] is False


def test_wait_for_runs_surfaces_a_broken_workspace_instead_of_calling_the_run_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken config must not be reported as "no such run".

    `ConfigError` subclasses `ValueError`, and the malformed-id handler catches `ValueError` - so
    without an explicit re-raise, a run that exists in a workspace whose config is broken comes back
    as `unknown`. That is the wrong answer to a different question, and it hides the fixable cause.
    """
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.core.config import ConfigError
    from marshal_engine.interfaces.mcp_server import build_app
    from marshal_engine.interfaces.workspaces import WorkspaceRegistry

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)

    def boom(self: Any, run_id: str, hint: str | None = None) -> Any:
        raise ConfigError("fleet.config.yaml is not parseable")

    monkeypatch.setattr(WorkspaceRegistry, "resolve_run", boom)
    app = build_app(build_service())

    with pytest.raises(Exception, match="not parseable"):
        asyncio.run(app.call_tool("wait_for_runs", {"run_ids": ["r-1"], "timeout_s": 1}))


def test_list_workflows_exposes_whether_a_recipe_writes_to_your_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auto: true` is the one place the "nothing reaches your branch unreviewed" property is given up.

    A driver is told to read a workflow before running it, and `list_workflows` is the only MCP
    surface that can show it - the YAML is off-surface. Emitting name+run alone left the single
    field that decides whether the driver's branch gets written to invisible. Asserted on the TOOL
    payload, not the service: the service always had `auto`, so a service-level assertion would
    have passed without the fix.
    """
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    wdir = repo / "workflows"
    wdir.mkdir()
    (wdir / "auto.yaml").write_text(
        "description: merges without review\n"
        "phases:\n"
        "  - name: build\n"
        "    run: agent\n"
        "    client: worker\n"
        "    goal: do it\n"
        "  - run: integrate\n"
        "    from_phase: build\n"
        "    auto: true\n"
    )
    (wdir / "gated.yaml").write_text(
        "description: reports candidates for review\n"
        "phases:\n"
        "  - name: build\n"
        "    run: agent\n"
        "    client: worker\n"
        "    goal: do it\n"
        "  - run: integrate\n"
        "    from_phase: build\n"
    )
    # `auto` on a phase the runner never reads it on. Nothing validates it away, so a recipe can
    # carry it - and it changes nothing, because the runner consults `auto` only when integrating.
    (wdir / "inert.yaml").write_text(
        "description: auto set where it does nothing\n"
        "phases:\n"
        "  - name: build\n"
        "    run: agent\n"
        "    client: worker\n"
        "    goal: do it\n"
        "    auto: true\n"
    )
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    structured = asyncio.run(app.call_tool("list_workflows", {})).structured_content
    payload = structured.get("result", structured) if isinstance(structured, dict) else structured
    by_name = {w["name"]: w for w in payload["workflows"]}

    assert by_name["auto"]["auto_integrates"] is True
    assert by_name["gated"]["auto_integrates"] is False
    # The flag answers "will my branch be written to", not "does the word appear in the YAML". A
    # warning that fires when nothing can happen is one a driver learns to skip past.
    assert by_name["inert"]["auto_integrates"] is False
    assert by_name["inert"]["phases"][0]["auto"] is True  # still reported verbatim per phase
    # per-phase too, so a driver can see WHICH phase merges and what it merges
    merging = [p for p in by_name["auto"]["phases"] if p["auto"]]
    assert len(merging) == 1
    assert merging[0]["run"] == "integrate"
    assert merging[0]["from_phase"] == "build"


# --- leaving the host's session (the suspended-host bug) --------------------------------------


def test_detach_from_host_terminal_leaves_the_session_when_spawned_over_a_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stdio host spawns us with stdin on a pipe. Staying in the host's process group is what lets
    # a terminal-touching descendant SIGTTOU the host into STAT=T, so we must leave the session.
    called: list[bool] = []
    monkeypatch.setattr(server_mod.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(server_mod.os, "setsid", lambda: called.append(True))
    assert server_mod.detach_from_host_terminal() is True
    assert called == [True], "setsid was never called; nothing detached us from the host"


def test_detach_from_host_terminal_is_skipped_on_a_real_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run by hand from a shell, detaching would take us out of the shell's job and break Ctrl-C.
    # The tty check is the whole discriminator, so assert setsid is not merely tolerated but unused.
    def boom() -> int:
        raise AssertionError("setsid must not run when stdin is a terminal")

    monkeypatch.setattr(server_mod.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(server_mod.os, "setsid", boom)
    assert server_mod.detach_from_host_terminal() is False


def test_detach_from_host_terminal_survives_setsid_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # EPERM means we already lead our process group - there is no host session to leave. Startup
    # must continue: a server that refuses to boot here is worse than one sharing a group.
    def eperm() -> int:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(server_mod.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(server_mod.os, "setsid", eperm)
    assert server_mod.detach_from_host_terminal() is False


def test_main_detaches_before_spawning_any_child() -> None:
    # Ordering is the invariant: merge_user_path() spawns an interactive login shell, and doing that
    # while still in the host's session is precisely the exposure detaching removes.
    src = inspect.getsource(server_mod.main)
    assert "detach_from_host_terminal()" in src
    assert src.index("detach_from_host_terminal()") < src.index("merge_user_path()")


def test_detach_from_host_terminal_when_stdin_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    # A closed or detached stdin is not a terminal, and probing it must not abort startup.
    class _Detached:
        def isatty(self) -> bool:
            raise ValueError("I/O operation on closed file")

    called: list[bool] = []
    monkeypatch.setattr(server_mod.sys, "stdin", _Detached())
    monkeypatch.setattr(server_mod.os, "setsid", lambda: called.append(True))
    assert server_mod.detach_from_host_terminal() is True

    monkeypatch.setattr(server_mod.sys, "stdin", None)
    assert server_mod.detach_from_host_terminal() is True
    assert called == [True, True]


# --- Streamable HTTP transport ----------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost"])
def test_require_loopback_accepts_only_this_machine(host: str) -> None:
    server_mod.require_loopback(host)  # must not raise


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.10", "10.0.0.4", "::", "example.com", "localhost.evil.com", ""],
)
def test_require_loopback_refuses_anything_reachable_off_box(host: str) -> None:
    # Marshal runs arbitrary commands, so a reachable endpoint is unauthenticated RCE. A hostname
    # is refused even when it would resolve to loopback: resolution can change after the check.
    with pytest.raises(ConfigError) as exc:
        server_mod.require_loopback(host)
    assert "ssh -L" in str(exc.value), "the refusal must name the supported way to reach it remotely"


def test_loopback_refusal_has_no_override_flag() -> None:
    # Invariant: binding off-box is refused outright, not gated behind a flag someone can set
    # once and forget. If that ever becomes a knob, this test should be the thing that argues it.
    src = inspect.getsource(server_mod.require_loopback) + inspect.getsource(server_mod.main)
    for escape in ("allow_remote", "allow-remote", "force", "insecure"):
        assert escape not in src, f"an override named {escape!r} appeared; loopback-only is the contract"


def test_http_transport_refuses_before_building_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bind check must precede the registry and the PATH probe: refusing after a 3s shell probe
    # and a workspace build wastes the operator's time and spawns children we did not need.
    def unexpected(*_a: object, **_k: object) -> None:
        raise AssertionError("startup work ran despite an illegal bind address")

    monkeypatch.setattr(server_mod, "merge_user_path", unexpected)
    monkeypatch.setattr(server_mod.WorkspaceRegistry, "from_env", unexpected)
    with pytest.raises(ConfigError):
        server_mod.main(transport="http", host="0.0.0.0")


def test_http_transport_does_not_leave_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # Detaching is a stdio-only concern: an HTTP server is nobody's child and has no host session.
    # Calling setsid anyway would take a foreground server out of its shell's job control.
    monkeypatch.setattr(
        server_mod, "detach_from_host_terminal",
        lambda: (_ for _ in ()).throw(AssertionError("http must not detach")),
    )
    monkeypatch.setattr(server_mod, "merge_user_path", lambda: False)
    monkeypatch.setattr(server_mod.WorkspaceRegistry, "from_env", lambda: _StubRegistry())
    ran: list[tuple[object, ...]] = []
    monkeypatch.setattr(server_mod, "build_app", lambda _r: _StubApp(ran))
    server_mod.main(transport="http", host="127.0.0.1", port=9999, path="/mcp")
    assert ran == [("streamable-http", {"host": "127.0.0.1", "port": 9999, "streamable_http_path": "/mcp"})]


def test_stdio_transport_still_detaches(monkeypatch: pytest.MonkeyPatch) -> None:
    detached: list[bool] = []
    monkeypatch.setattr(server_mod, "detach_from_host_terminal", lambda: detached.append(True))
    monkeypatch.setattr(server_mod, "merge_user_path", lambda: False)
    monkeypatch.setattr(server_mod.WorkspaceRegistry, "from_env", lambda: _StubRegistry())
    ran: list[tuple[object, ...]] = []
    monkeypatch.setattr(server_mod, "build_app", lambda _r: _StubApp(ran))
    server_mod.main()
    assert detached == [True]
    assert ran == [((), {})], "stdio must run the default transport, with no HTTP kwargs"


class _StubRegistry:
    def get(self, _name: str) -> object:
        return object()


class _StubApp:
    def __init__(self, sink: list[tuple[object, ...]]) -> None:
        self._sink = sink

    def run(self, *args: object, **kwargs: object) -> None:
        self._sink.append((args[0] if args else (), kwargs))


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "http://127.0.0.1:8765/mcp"),
        ("localhost", "http://localhost:8765/mcp"),
        # Bracketed: without them the address's colons are indistinguishable from the port
        # separator, and the URL an operator copies into a client config does not resolve.
        ("::1", "http://[::1]:8765/mcp"),
    ],
)
def test_endpoint_url_is_a_usable_url(host: str, expected: str) -> None:
    url = server_mod.endpoint_url(host, 8765, "/mcp")
    assert url == expected
    parsed = urllib.parse.urlparse(url)
    assert parsed.port == 8765, f"{url} does not parse back to the port we bound"
    assert parsed.hostname == host.strip("[]")


def test_announced_url_goes_through_endpoint_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The banner is the one string an operator copies. Building it inline again is how the IPv6
    # form drifts back out of sync with the helper that gets it right.
    src = inspect.getsource(server_mod.main)
    assert "endpoint_url(host, port, path)" in src
    assert "http://{host}" not in src


def test_wait_for_runs_defaults_to_the_poll_shape_and_honours_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION (#257.8): wait_for_runs dumped whole records - up to 16k of `text` each, times a
    fan-out's worth of runs - while `status` deliberately avoids exactly that, and the quickstart
    steers drivers from `status` to here. It now shares `status`' three shapes, defaulting to
    `poll`, and the trimmed views carry `has_text` so an omission never reads as an absence."""
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.interfaces.mcp_server import build_app
    from marshal_engine.runtime.state import FleetState, RunRecord

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    state = FleetState(repo / ".marshal" / "runs")
    state.add(
        RunRecord(
            run_id="r-wordy", task_id="t", backend="echo", status="exited_clean",
            text="x" * 5000,
        )
    )
    app = build_app(build_service())

    def _wait(**kw: object) -> dict:
        got = asyncio.run(
            app.call_tool("wait_for_runs", {"run_ids": ["r-wordy"], "timeout_s": 1, **kw})
        ).structured_content
        assert got is not None
        return got

    default = _wait()
    assert default["view"] == "poll"
    settled = default["settled"][0]
    assert "text" not in settled, "the polling default still dumped the unbounded message"
    assert settled["has_text"] is True, "an omitted field must not read as an empty one"

    full = _wait(view="full")
    assert full["view"] == "full"
    assert full["settled"][0]["text"] == "x" * 5000, "view='full' must still give the whole text"


# --- the then-stage carries the same inputs as its primary ----------------------------------


def test_a_then_stage_declares_every_field_its_primary_does() -> None:
    """A field on `Job` and not on `ThenJob` is dropped in silence.

    Pydantic ignores unknown keys and the engine's `job_request` reads the same names off both
    bodies, so the follow-up spawns without the input the driver asked for and still reports
    success. `read_paths` and `artifacts_from` were missing for exactly that reason, turning two
    documented fail-closed guarantees into no-ops. Compared by field set rather than by listing
    the names, so a field added to `Job` later cannot quietly skip the follow-up.
    """
    missing = (set(Job.model_fields) - {"then", "workspace"}) - set(ThenJob.model_fields)
    assert not missing, (
        f"ThenJob is missing {sorted(missing)}; a driver passing those to `then` would have them "
        "silently dropped and the follow-up would run without them"
    )


def test_a_then_stage_keeps_the_context_inputs_through_validation() -> None:
    """The parity check above is structural; this one proves the values actually survive."""
    job = Job.model_validate(
        {
            "goal": "primary",
            "then": {
                "goal": "follow-up",
                "read_paths": ["/tmp/notes.md"],
                "artifacts_from": ["run-1"],
            },
        }
    )
    assert job.then is not None
    body = job.then.model_dump()
    assert body["read_paths"] == ["/tmp/notes.md"]
    assert body["artifacts_from"] == ["run-1"]
