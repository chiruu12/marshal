"""Tests for declarative workflows - pure spec/validation + the runner over a stub service.

The runner is exercised against a StubService that records every call and returns canned records,
so no Fleet, git, or process is involved. The StubService exposes ONLY the four primitives the
runner is permitted to use, which encodes the "no new execution path" invariant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from marshal_engine.config import ClientConfig, ConfigError, FleetConfig
from marshal_engine.fleet import CollectResult, IntegrateResult
from marshal_engine.state import RunRecord
from marshal_engine.workflow import (
    PhaseSpec,
    WorkflowRunner,
    WorkflowSpec,
    find_workflow,
    list_workflows,
    load_workflow,
    render_goal,
    resolve_source,
    validate_workflow,
)


def _config(*names: str) -> FleetConfig:
    return FleetConfig(clients={n: ClientConfig(name=n, backend="opencode") for n in names})


class StubService:
    """A service stand-in: records calls, returns canned records. Only the four runner primitives."""

    def __init__(
        self,
        config: FleetConfig,
        *,
        statuses: dict[str, str] | None = None,
        collect_errors: set[str] | None = None,
        integrate: dict[str, IntegrateResult] | None = None,
        unavailable: set[str] | None = None,
    ) -> None:
        self.config = config
        self.calls: list[tuple[Any, ...]] = []
        self._statuses = statuses or {}
        self._collect_errors = collect_errors or set()
        self._integrate = integrate or {}
        self._unavailable = unavailable
        self._n = 0

    def client_available(self, name: str) -> bool:
        return name not in (self._unavailable or set())

    def _make(self, client: str, task_id: str | None) -> RunRecord:
        self._n += 1
        return RunRecord(
            run_id=f"{client}.{self._n}",
            task_id=task_id or "t",
            backend="opencode",
            client=client,
            status=self._statuses.get(client, "succeeded"),
        )

    def run_many(
        self, jobs: list[dict[str, Any]], *, max_concurrency: int = 4
    ) -> list[RunRecord]:
        self.calls.append(("run_many", max_concurrency, [j["client"] for j in jobs], jobs[0]["task_id"]))
        return [self._make(j["client"], j.get("task_id")) for j in jobs]

    def run_agent(self, client_name: str, goal: str, *, task_id: str | None = None) -> RunRecord:
        self.calls.append(("run_agent", client_name, task_id))
        return self._make(client_name, task_id)

    def collect_run(self, run_id: str) -> CollectResult:
        self.calls.append(("collect_run", run_id))
        if run_id in self._collect_errors:
            raise ValueError(f"run {run_id}: no collectable work")
        return CollectResult(
            run_id=run_id, branch="b", worktree="w", changed_files=["f.py"], diff="--- diff ---"
        )

    def integrate(self, run_id: str, *, cleanup: bool = False) -> IntegrateResult:
        self.calls.append(("integrate", run_id, cleanup))
        return self._integrate.get(
            run_id, IntegrateResult(run_id=run_id, status="merged", merged_into="main")
        )


# --- pure: render / resolve / validate ---------------------------------------------------------


def test_render_goal_strict() -> None:
    assert render_goal("review {target}", {"target": "src/x.py"}) == "review src/x.py"
    assert render_goal("literal {{braces}}", {}) == "literal {braces}"
    with pytest.raises(ConfigError):
        render_goal("review {missing}", {})


def test_resolve_source_defaults_to_most_recent_generative() -> None:
    spec = WorkflowSpec(
        name="w",
        phases=[
            PhaseSpec(name="a", run="fan_out", clients=["x"], goal="g"),
            PhaseSpec(run="collect"),
            PhaseSpec(name="b", run="fan_out", clients=["x"], goal="g"),
            PhaseSpec(run="integrate"),
        ],
    )
    assert resolve_source(spec, 1) == 0
    assert resolve_source(spec, 3) == 2


def test_resolve_source_from_phase_targets_named_generative() -> None:
    spec = WorkflowSpec(
        name="w",
        phases=[
            PhaseSpec(name="first", run="fan_out", clients=["x"], goal="g"),
            PhaseSpec(name="second", run="fan_out", clients=["x"], goal="g"),
            PhaseSpec(run="integrate", from_phase="first"),
        ],
    )
    assert resolve_source(spec, 2) == 0  # from_phase overrides "most recent"


def test_from_phase_must_name_earlier_generative_phase() -> None:
    spec = WorkflowSpec(
        name="w",
        phases=[
            PhaseSpec(name="g", run="fan_out", clients=["x"], goal="g"),
            PhaseSpec(run="integrate", from_phase="nope"),
        ],
    )
    with pytest.raises(ConfigError):
        resolve_source(spec, 1)


def test_validate_unknown_client_rejected() -> None:
    spec = WorkflowSpec(
        name="w", phases=[PhaseSpec(run="fan_out", clients=["ghost"], goal="g")]
    )
    with pytest.raises(ConfigError, match="unknown client.*hint: client names come from"):
        validate_workflow(spec, _config("real"))


def test_validate_fan_out_requires_clients_and_goal() -> None:
    with pytest.raises(ConfigError):
        validate_workflow(WorkflowSpec(name="w", phases=[PhaseSpec(run="fan_out", goal="g")]), _config())
    with pytest.raises(ConfigError):
        validate_workflow(
            WorkflowSpec(name="w", phases=[PhaseSpec(run="fan_out", clients=["x"])]), _config("x")
        )


def test_validate_agent_requires_client_and_goal() -> None:
    with pytest.raises(ConfigError):
        validate_workflow(WorkflowSpec(name="w", phases=[PhaseSpec(run="agent", goal="g")]), _config())


def test_validate_integrate_without_source_rejected() -> None:
    spec = WorkflowSpec(name="w", phases=[PhaseSpec(run="integrate")])
    with pytest.raises(ConfigError):
        validate_workflow(spec, _config())


def test_validate_goal_with_undeclared_input_rejected() -> None:
    spec = WorkflowSpec(
        name="w", inputs=["target"], phases=[PhaseSpec(run="fan_out", clients=["x"], goal="{other}")]
    )
    with pytest.raises(ConfigError, match="undeclared"):
        validate_workflow(spec, _config("x"))


@pytest.mark.parametrize("goal", ["{target.__class__}", "{target[0]}", "do {}"])
def test_validate_rejects_non_bare_placeholders(goal: str) -> None:
    # attribute/index access and positional {} would bypass input validation / leak internals.
    spec = WorkflowSpec(
        name="w", inputs=["target"], phases=[PhaseSpec(run="fan_out", clients=["x"], goal=goal)]
    )
    with pytest.raises(ConfigError):
        validate_workflow(spec, _config("x"))


# --- loading / discovery -----------------------------------------------------------------------


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_minimal_workflow(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "review.yaml",
        "description: r\ninputs: [target]\nphases:\n  - run: fan_out\n    clients: [a]\n    goal: 'do {target}'\n",
    )
    spec = load_workflow(p)
    assert spec.name == "review"  # defaults to the file stem
    assert spec.inputs == ["target"]
    assert spec.phases[0].run == "fan_out"


def test_load_rejects_unknown_key(tmp_path: Path) -> None:
    p = _write(tmp_path / "bad.yaml", "phases:\n  - run: fan_out\n    cleints: [a]\n    goal: g\n")
    with pytest.raises(ConfigError):
        load_workflow(p)


def test_find_and_list_workflows(tmp_path: Path) -> None:
    wdir = tmp_path / "workflows"
    wdir.mkdir()
    _write(wdir / "review.yaml", "phases:\n  - run: fan_out\n    clients: [a]\n    goal: g\n")
    _write(wdir / "broken.yaml", "phases:\n  - run: not_a_real_kind\n")
    assert find_workflow("review", wdir).name == "review.yaml"
    with pytest.raises(ConfigError):
        find_workflow("missing", wdir)
    # list skips the malformed file rather than raising
    names = [w.name for w in list_workflows(wdir)]
    assert names == ["review"]


# --- runner ------------------------------------------------------------------------------------


def _review_spec() -> WorkflowSpec:
    return WorkflowSpec(
        name="review",
        inputs=["target"],
        phases=[
            PhaseSpec(name="review", run="fan_out", clients=["a", "b"], goal="check {target}"),
            PhaseSpec(name="gate", run="collect"),
            PhaseSpec(name="merge", run="integrate"),
        ],
    )


def test_runner_gated_integrate_never_merges() -> None:
    svc = StubService(_config("a", "b"))
    result = WorkflowRunner(svc).run(_review_spec(), {"target": "src/x.py"})

    assert result.status == "awaiting_review"
    assert not any(c[0] == "integrate" for c in svc.calls)  # gate: integrate is never called
    merge_phase = result.phases[-1]
    assert len(merge_phase.skipped) == 2  # both succeeded runs handed back for review
    assert any("integrate" in a for a in result.next_actions)


def test_runner_auto_integrate_only_succeeded() -> None:
    spec = WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[
            PhaseSpec(name="impl", run="fan_out", clients=["a", "b"], goal="{t}"),
            PhaseSpec(name="merge", run="integrate", auto=True),
        ],
    )
    svc = StubService(_config("a", "b"), statuses={"a": "succeeded", "b": "failed"})
    result = WorkflowRunner(svc).run(spec, {"t": "go"})

    integrated = [c[1] for c in svc.calls if c[0] == "integrate"]
    assert integrated == ["a.1"]  # only the succeeded run was integrated; the failed one skipped
    assert result.status == "completed"


def test_runner_collect_survives_a_bad_run() -> None:
    spec = WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[
            PhaseSpec(name="impl", run="fan_out", clients=["a", "b"], goal="{t}"),
            PhaseSpec(name="gate", run="collect"),
        ],
    )
    svc = StubService(_config("a", "b"), collect_errors={"a.1"})
    result = WorkflowRunner(svc).run(spec, {"t": "go"})

    gate = result.phases[1]
    assert len(gate.collected) == 1          # b.2 collected fine
    assert any("a.1" in n for n in gate.notes)  # a.1's raise was recorded, run continued


def test_runner_auto_integrate_conflict_is_awaiting_review() -> None:
    spec = WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[
            PhaseSpec(name="impl", run="fan_out", clients=["a"], goal="{t}"),
            PhaseSpec(name="merge", run="integrate", auto=True),
        ],
    )
    conflict = IntegrateResult(run_id="a.1", status="conflict", conflicts=["f.py"])
    svc = StubService(_config("a"), integrate={"a.1": conflict})
    assert WorkflowRunner(svc).run(spec, {"t": "go"}).status == "awaiting_review"


def _auto_integrate_spec() -> WorkflowSpec:
    return WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[
            PhaseSpec(name="impl", run="fan_out", clients=["a", "b"], goal="{t}"),
            PhaseSpec(name="merge", run="integrate", auto=True),
        ],
    )


def test_runner_auto_integrate_all_merged_is_completed() -> None:
    svc = StubService(_config("a", "b"))  # both succeed, both merge (StubService default)
    result = WorkflowRunner(svc).run(_auto_integrate_spec(), {"t": "go"})

    assert result.status == "completed"
    assert [c[1] for c in svc.calls if c[0] == "integrate"] == ["a.1", "b.2"]
    assert result.next_actions == []  # clean merge needs no follow-up


def test_runner_auto_integrate_blocked_surfaces_message() -> None:
    blocked = IntegrateResult(run_id="a.1", status="blocked", message="working tree is dirty")
    svc = StubService(_config("a", "b"), integrate={"a.1": blocked})
    result = WorkflowRunner(svc).run(_auto_integrate_spec(), {"t": "go"})

    assert result.status == "awaiting_review"
    assert any("working tree is dirty" in a and "a.1" in a for a in result.next_actions)


def test_runner_auto_integrate_empty_is_completed_with_note() -> None:
    empty = IntegrateResult(run_id="a.1", status="empty")
    svc = StubService(_config("a", "b"), integrate={"a.1": empty})
    result = WorkflowRunner(svc).run(_auto_integrate_spec(), {"t": "go"})

    assert result.status == "completed"  # nothing landed, nothing to review - not a gate
    merge_phase = result.phases[-1]
    assert any("a.1" in n for n in merge_phase.notes)
    assert not any("a.1" in a for a in result.next_actions)  # no action demanded for an empty merge


def test_runner_auto_integrate_error_is_error_status() -> None:
    spec = WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[
            PhaseSpec(name="impl", run="fan_out", clients=["a"], goal="{t}"),
            PhaseSpec(name="merge", run="integrate", auto=True),
        ],
    )
    err = IntegrateResult(run_id="a.1", status="error", message="git blew up")
    svc = StubService(_config("a"), integrate={"a.1": err})
    assert WorkflowRunner(svc).run(spec, {"t": "go"}).status == "error"


def test_runner_per_phase_task_id_grouping() -> None:
    spec = WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[
            PhaseSpec(name="first", run="fan_out", clients=["a", "b"], goal="{t}"),
            PhaseSpec(name="second", run="fan_out", clients=["a"], goal="{t}"),
        ],
    )
    svc = StubService(_config("a", "b"))
    result = WorkflowRunner(svc).run(spec, {"t": "go"})

    wfid = result.workflow_run_id
    t1 = {r["task_id"] for r in result.phases[0].records}
    t2 = {r["task_id"] for r in result.phases[1].records}
    assert t1 == {f"{wfid}.first"}      # the two clients in phase 1 share one task_id
    assert t2 == {f"{wfid}.second"}     # distinct from phase 2's
    assert t1 != t2


def test_runner_validates_before_running() -> None:
    spec = WorkflowSpec(name="w", phases=[PhaseSpec(run="fan_out", clients=["ghost"], goal="g")])
    svc = StubService(_config("real"))
    with pytest.raises(ConfigError, match="unknown client"):
        WorkflowRunner(svc).run(spec, {})
    assert svc.calls == []  # nothing ran - validation failed fast


def test_runner_missing_input_raises_before_running() -> None:
    svc = StubService(_config("a", "b"))
    with pytest.raises(ConfigError, match="missing input"):
        WorkflowRunner(svc).run(_review_spec(), {})  # no 'target'
    assert svc.calls == []


def test_runner_uses_only_the_four_primitives() -> None:
    svc = StubService(_config("a", "b"))
    WorkflowRunner(svc).run(_review_spec(), {"target": "x"})
    assert {c[0] for c in svc.calls} <= {"run_many", "run_agent", "collect_run", "integrate"}


# --- runner: unavailable-client skipping + failure surfacing -----------------------------------


def test_runner_fan_out_skips_unavailable_client() -> None:
    # (a) one of two clients is unavailable; run_many receives only the available client
    spec = WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[PhaseSpec(name="impl", run="fan_out", clients=["a", "b"], goal="{t}")],
    )
    svc = StubService(_config("a", "b"), unavailable={"b"})
    result = WorkflowRunner(svc).run(spec, {"t": "go"})

    # only "a" reached run_many
    run_many_calls = [c for c in svc.calls if c[0] == "run_many"]
    assert run_many_calls[0][2] == ["a"]  # the recorded client list excludes "b"
    # a skip note mentions the unavailable client
    impl = result.phases[0]
    assert any("b" in n and "skipped" in n for n in impl.notes)
    assert result.status == "completed"  # the available run succeeded, nothing to review


def test_runner_fan_out_all_unavailable_raises() -> None:
    # (b) every client unavailable -> ConfigError, and nothing spawns
    spec = WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[PhaseSpec(name="impl", run="fan_out", clients=["a", "b"], goal="{t}")],
    )
    svc = StubService(_config("a", "b"), unavailable={"a", "b"})
    with pytest.raises(ConfigError, match="all clients unavailable"):
        WorkflowRunner(svc).run(spec, {"t": "go"})
    assert svc.calls == []  # nothing ran


def test_runner_fan_out_failed_run_surfaces_note_and_action() -> None:
    # (c) a run that does not succeed records a note + a next_action (additive, status unchanged)
    spec = WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[PhaseSpec(name="impl", run="fan_out", clients=["a"], goal="{t}")],
    )
    svc = StubService(_config("a"), statuses={"a": "failed"})
    result = WorkflowRunner(svc).run(spec, {"t": "go"})

    impl = result.phases[0]
    assert any("a.1" in n and "did not succeed" in n for n in impl.notes)
    assert any("a.1" in action and "failed" in action for action in result.next_actions)


# --- runtime invariants: a malformed phase must fail with a clear error, not crash ----------


def test_runner_agent_phase_without_client_raises_config_error() -> None:
    # `run: agent` requires a `client` (validate_workflow enforces this for spec users). If a
    # caller ever constructs a WorkflowSpec programmatically and bypasses validation, the
    # runner must still fail with a clean ConfigError - not a TypeError on `None.client`,
    # and not a vanished assert under `python -O`.
    spec = WorkflowSpec(
        name="w",
        phases=[PhaseSpec(name="bare", run="agent", goal="go")],  # no `client`
    )
    svc = StubService(_config("a"))
    with pytest.raises(ConfigError, match="missing 'client'"):
        WorkflowRunner(svc).run(spec, {})
    assert svc.calls == []  # nothing ran - the check fired before any primitive


def test_runner_survives_service_run_many_failure() -> None:
    # A phase's primitive raising (e.g. `run_many` failing because no configured client exists
    # at runtime) must NOT abort the whole workflow. The runner records the failure as a phase
    # note + a `next_action` and keeps going so the driver sees the full picture.
    spec = WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[
            PhaseSpec(name="impl", run="fan_out", clients=["a"], goal="{t}"),
            PhaseSpec(name="collect", run="collect"),
        ],
    )

    class _RaisingService(StubService):
        def run_many(self, jobs, *, max_concurrency=4):  # noqa: ANN001, ARG002
            raise ConfigError("simulated backend blowup")

    svc = _RaisingService(_config("a"))
    result = WorkflowRunner(svc).run(spec, {"t": "go"})

    impl = result.phases[0]
    assert any("simulated backend blowup" in n for n in impl.notes)
    assert any("simulated backend blowup" in a for a in result.next_actions)
    # the workflow did NOT raise - the failure is captured, not propagated
    assert result.status == "error"


def test_runner_survives_service_run_agent_failure() -> None:
    # Same contract for `run: agent` phases: the service's run_agent raising must surface as
    # a phase note + next_action, not crash the workflow.
    spec = WorkflowSpec(
        name="w",
        inputs=["t"],
        phases=[PhaseSpec(name="impl", run="agent", client="a", goal="{t}")],
    )

    class _RaisingService(StubService):
        def run_agent(self, client_name, goal, *, task_id=None):  # noqa: ANN001, ARG002
            raise ValueError("client vanished")

    svc = _RaisingService(_config("a"))
    result = WorkflowRunner(svc).run(spec, {"t": "go"})
    impl = result.phases[0]
    assert any("client vanished" in n for n in impl.notes)
    assert any("client vanished" in a for a in result.next_actions)
