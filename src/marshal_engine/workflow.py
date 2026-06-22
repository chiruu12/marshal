"""Declarative YAML workflows - reusable, human-authored fleet orchestration recipes.

A workflow is a named sequence of *phases* the driver can run by name instead of re-deriving the
plan each time: fan out a goal across clients, collect the diffs, and (opt-in) integrate the clean
ones. It is the engine analogue of an "ultracode" workflow, scoped to Marshal's primitives.

Safety property (the reason this is allowed to live in the engine despite the
"engine is mechanism, judgment in Skills" invariant): **the WorkflowRunner adds no new execution
path.** It issues exactly the calls a human driver would make by hand - ``run_many`` / ``run_agent``
/ ``collect_run`` / ``integrate`` - in a declared order. Every run still flows through ``Fleet.run``
(external timeout + process-group kill + usage ledger + worktree). The runner never spawns a
process, touches git, or writes run state. Integration is **gated off by default** (``auto: false``)
so main is untouched until an explicit, reviewed merge - ``succeeded`` is not ``correct``.

Spec parsing and validation are pure (no spawning), so a typo'd recipe fails before any agent runs.
"""

from __future__ import annotations

import string
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from .config import ConfigError, FleetConfig

if TYPE_CHECKING:  # typing only - avoids a runtime import cycle with fleet/state
    from .fleet import CollectResult, IntegrateResult
    from .state import RunRecord

_GENERATIVE = ("fan_out", "agent")


# --- spec ---------------------------------------------------------------------------------


class PhaseSpec(BaseModel):
    """One step in a workflow.

    ``fan_out`` runs ``goal`` across ``clients`` in parallel; ``agent`` runs it on one ``client``;
    ``collect`` surfaces a prior phase's diffs (read-only); ``integrate`` merges them - but only if
    ``auto`` is true (default false = report candidates for the driver to merge after review).
    """

    model_config = ConfigDict(extra="forbid")  # a typo'd key (e.g. 'cleints') is a load error

    name: str | None = None
    run: Literal["fan_out", "agent", "collect", "integrate"]
    clients: list[str] = []          # fan_out
    client: str | None = None        # agent
    goal: str | None = None          # fan_out/agent - template with {input} substitution
    auto: bool = False               # integrate gate; default OFF (safety)
    from_phase: str | None = None    # collect/integrate source (names an earlier named generative phase)


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    inputs: list[str] = []
    phases: list[PhaseSpec]


# --- results (JSON-serializable for the MCP surface) --------------------------------------


class PhaseResult(BaseModel):
    name: str | None
    run: str
    run_ids: list[str] = []                 # generative phases
    records: list[dict[str, Any]] = []      # RunRecord dumps for generative phases
    collected: list[dict[str, Any]] = []    # CollectResult dumps for collect
    integrations: list[dict[str, Any]] = [] # IntegrateResult dumps for integrate
    skipped: list[str] = []                 # run_ids not acted on (gate off / not a candidate)
    notes: list[str] = []                   # per-run issues (e.g. collect on a failed run)


class WorkflowResult(BaseModel):
    name: str
    workflow_run_id: str
    inputs: dict[str, str]
    phases: list[PhaseResult]
    status: Literal["completed", "awaiting_review", "error"]
    next_actions: list[str] = []


# --- pure helpers -------------------------------------------------------------------------


def _placeholders(template: str) -> set[str]:
    """The set of ``{name}`` field names referenced in a template.

    Strict: only bare ``{name}`` placeholders are allowed. A positional ``{}``/``{0}``, or any
    attribute/index access (``{x.attr}``, ``{x[0]}``), raises ValueError - those would slip past
    input-declaration checks and leak object internals at render time. Bad braces also raise.
    """
    names: set[str] = set()
    for _literal, field, _spec, _conv in string.Formatter().parse(template):
        if field is None:  # trailing literal text - no placeholder here
            continue
        if field == "":
            raise ValueError("positional {} placeholder is not allowed; use a named {input}")
        if "." in field or "[" in field:
            raise ValueError(
                f"placeholder {{{field}}} must be a bare input name (no attribute/index access)"
            )
        names.add(field)
    return names


def render_goal(template: str, inputs: dict[str, str]) -> str:
    """Substitute ``{input}`` tokens. Strict: an undeclared/missing token raises ConfigError, never
    a KeyError, and there is no arbitrary evaluation."""

    class _Strict(dict):  # type: ignore[type-arg]
        def __missing__(self, key: str) -> str:
            raise ConfigError(f"goal references unknown input {{{key}}}")

    try:
        return template.format_map(_Strict(inputs))
    except (ValueError, IndexError) as exc:
        raise ConfigError(f"invalid goal template {template!r}: {exc}") from exc


def resolve_source(spec: WorkflowSpec, phase_index: int) -> int:
    """Index of the generative phase a collect/integrate phase sources from (pure; declaration order).

    If ``from_phase`` is set it must name exactly one earlier ``fan_out``/``agent`` phase. Otherwise
    the most recent preceding generative phase is used. Raises ConfigError if none resolves.
    """
    phase = spec.phases[phase_index]
    if phase.from_phase is not None:
        matches = [
            i
            for i in range(phase_index)
            if spec.phases[i].run in _GENERATIVE and spec.phases[i].name == phase.from_phase
        ]
        if not matches:
            raise ConfigError(
                f"phase {phase_index} ({phase.run}): from_phase {phase.from_phase!r} is not an "
                "earlier fan_out/agent phase (a from_phase target must be named and generative)"
            )
        if len(matches) > 1:
            raise ConfigError(
                f"phase {phase_index} ({phase.run}): from_phase {phase.from_phase!r} is ambiguous "
                f"(matches {len(matches)} phases); give them distinct names"
            )
        return matches[0]
    for i in range(phase_index - 1, -1, -1):
        if spec.phases[i].run in _GENERATIVE:
            return i
    raise ConfigError(
        f"phase {phase_index} ({phase.run}): no preceding fan_out/agent phase to source from; "
        "add one before it or set from_phase"
    )


def validate_workflow(spec: WorkflowSpec, config: FleetConfig) -> None:
    """Raise ConfigError on any structural problem - BEFORE any agent runs (fail-fast like run_many)."""
    if not spec.phases:
        raise ConfigError(f"workflow {spec.name!r}: has no phases")
    known = set(config.clients)
    declared = set(spec.inputs)
    for idx, phase in enumerate(spec.phases):
        if phase.run in _GENERATIVE:
            if not phase.goal:
                raise ConfigError(f"phase {idx} ({phase.run}): missing 'goal'")
            try:
                refs = _placeholders(phase.goal)
            except ValueError as exc:
                raise ConfigError(f"phase {idx} ({phase.run}): malformed goal template: {exc}") from exc
            undeclared = sorted(refs - declared)
            if undeclared:
                raise ConfigError(
                    f"phase {idx} ({phase.run}): goal references undeclared inputs {undeclared}; "
                    "declare them under 'inputs'"
                )
            targets = phase.clients if phase.run == "fan_out" else ([phase.client] if phase.client else [])
            if not targets:
                need = "clients" if phase.run == "fan_out" else "client"
                raise ConfigError(f"phase {idx} ({phase.run}): missing '{need}'")
            unknown = [c for c in targets if c not in known]
            if unknown:
                listed = ", ".join(sorted(known)) or "(none configured)"
                raise ConfigError(
                    f"phase {idx} ({phase.run}): unknown client(s) {unknown}; configured: {listed}"
                )
        else:  # collect | integrate - must resolve a source phase (raises if it can't)
            resolve_source(spec, idx)


# --- discovery ----------------------------------------------------------------------------


def load_workflow(path: Path | str) -> WorkflowSpec:
    """Parse a workflow YAML file into a WorkflowSpec (structural validation only)."""
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"workflow {p}: cannot read/parse: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"workflow {p}: top-level must be a mapping")
    raw.setdefault("name", p.stem)
    try:
        return WorkflowSpec.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"workflow {p}: invalid: {exc}") from exc


def find_workflow(name: str, directory: Path | str) -> Path:
    """Locate ``<directory>/<name>.yaml`` (or ``.yml``). Raises ConfigError if absent."""
    d = Path(directory)
    for ext in (".yaml", ".yml"):
        candidate = d / f"{name}{ext}"
        if candidate.exists():
            return candidate
    raise ConfigError(f"no workflow {name!r} in {d}; create {d / (name + '.yaml')} (see examples/workflows/)")


def workflow_paths(directory: Path | str) -> list[Path]:
    """Every workflow file in a directory (``*.yaml`` / ``*.yml``), sorted by name."""
    d = Path(directory)
    if not d.exists():
        return []
    return sorted([*d.glob("*.yaml"), *d.glob("*.yml")], key=lambda p: p.name)


def list_workflows(directory: Path | str) -> list[WorkflowSpec]:
    """All parseable workflows in a directory (malformed files are skipped, not raised)."""
    specs: list[WorkflowSpec] = []
    for p in workflow_paths(directory):
        try:
            specs.append(load_workflow(p))
        except ConfigError:
            continue
    return specs


# --- runner -------------------------------------------------------------------------------


class WorkflowService(Protocol):
    """The slice of MarshalService the runner uses - and the *only* calls it is permitted to make.

    Typing the runner against this Protocol (not the concrete service) keeps it decoupled and makes
    the "no new execution path" property checkable: a stub satisfying exactly these four methods is
    enough to drive a whole workflow in a test, with no Fleet, git, or processes.
    """

    config: FleetConfig

    def run_many(self, jobs: list[dict[str, Any]], *, max_concurrency: int = 4) -> list[RunRecord]: ...
    def run_agent(self, client_name: str, goal: str, *, task_id: str | None = None) -> RunRecord: ...
    def collect_run(self, run_id: str) -> CollectResult: ...
    def integrate(self, run_id: str, *, cleanup: bool = False) -> IntegrateResult: ...


class WorkflowRunner:
    """Sequences a validated WorkflowSpec over service primitives. Adds no execution path."""

    def __init__(self, service: WorkflowService) -> None:
        self.service = service

    def run(
        self, spec: WorkflowSpec, inputs: dict[str, Any], *, max_concurrency: int = 4
    ) -> WorkflowResult:
        # Validate the whole recipe up front so a bad reference fails BEFORE any agent spawns
        # (fail-fast, like run_many's client check). Inputs are coerced to str for templating.
        validate_workflow(spec, self.service.config)
        inputs = {k: str(v) for k, v in (inputs or {}).items()}
        missing = [i for i in spec.inputs if i not in inputs]
        if missing:
            raise ConfigError(f"workflow {spec.name!r}: missing input(s): {', '.join(missing)}")

        workflow_run_id = uuid.uuid4().hex[:8]
        runs_by_index: dict[int, list[str]] = {}
        status_by_run: dict[str, str] = {}
        phases: list[PhaseResult] = []
        next_actions: list[str] = []
        had_error = False
        needs_review = False

        for idx, phase in enumerate(spec.phases):
            label = phase.name or f"{phase.run}{idx}"
            if phase.run == "fan_out":
                goal = render_goal(phase.goal or "", inputs)
                task_id = f"{workflow_run_id}.{label}"
                jobs = [{"client": c, "goal": goal, "task_id": task_id} for c in phase.clients]
                records = self.service.run_many(jobs, max_concurrency=max_concurrency)
                run_ids = [r.run_id for r in records]
                runs_by_index[idx] = run_ids
                for r in records:
                    status_by_run[r.run_id] = r.status
                phases.append(
                    PhaseResult(
                        name=phase.name,
                        run=phase.run,
                        run_ids=run_ids,
                        records=[r.model_dump(mode="json") for r in records],
                    )
                )
            elif phase.run == "agent":
                assert phase.client is not None  # validate_workflow guarantees this
                goal = render_goal(phase.goal or "", inputs)
                task_id = f"{workflow_run_id}.{label}"
                rec = self.service.run_agent(phase.client, goal, task_id=task_id)
                runs_by_index[idx] = [rec.run_id]
                status_by_run[rec.run_id] = rec.status
                phases.append(
                    PhaseResult(
                        name=phase.name,
                        run=phase.run,
                        run_ids=[rec.run_id],
                        records=[rec.model_dump(mode="json")],
                    )
                )
            elif phase.run == "collect":
                source_ids = runs_by_index[resolve_source(spec, idx)]
                collected: list[dict[str, Any]] = []
                notes: list[str] = []
                for rid in source_ids:
                    try:
                        cr = self.service.collect_run(rid)
                    except ValueError as exc:  # empty/failed/missing-worktree runs raise; record, continue
                        notes.append(f"{rid}: {exc}")
                        continue
                    collected.append(cr.model_dump(mode="json"))
                    if cr.changed_files:
                        needs_review = True
                        next_actions.append(f"review diff: {rid} ({len(cr.changed_files)} file(s))")
                phases.append(
                    PhaseResult(name=phase.name, run=phase.run, collected=collected, notes=notes)
                )
            else:  # integrate
                source_ids = runs_by_index[resolve_source(spec, idx)]
                candidates = [rid for rid in source_ids if status_by_run.get(rid) == "succeeded"]
                pr = PhaseResult(name=phase.name, run=phase.run)
                if not phase.auto:
                    # default safety gate: never merge automatically; hand candidates to the driver.
                    pr.skipped = candidates
                    for rid in candidates:
                        next_actions.append(f"integrate after review (or skip): {rid}")
                    if candidates:
                        needs_review = True
                else:
                    for rid in candidates:
                        ir = self.service.integrate(rid)
                        pr.integrations.append(ir.model_dump(mode="json"))
                        if ir.status == "error":
                            had_error = True
                            next_actions.append(f"integrate error (human needed): {rid}: {ir.message}")
                        elif ir.status == "conflict":
                            needs_review = True
                            next_actions.append(
                                f"resolve conflict then retry: {rid} ({', '.join(ir.conflicts)})"
                            )
                        elif ir.status == "blocked":
                            needs_review = True
                            next_actions.append(
                                f"integrate blocked, fix then retry: {rid}: {ir.message}"
                            )
                        elif ir.status == "empty":
                            # nothing landed and nothing to review - informational, not a gate.
                            pr.notes.append(f"{rid}: nothing to integrate (empty)")
                        # "merged" → no follow-up needed
                phases.append(pr)

        status: Literal["completed", "awaiting_review", "error"] = (
            "error" if had_error else "awaiting_review" if needs_review else "completed"
        )
        return WorkflowResult(
            name=spec.name,
            workflow_run_id=workflow_run_id,
            inputs=inputs,
            phases=phases,
            status=status,
            next_actions=next_actions,
        )
