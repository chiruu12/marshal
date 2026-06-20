"""Core data types shared across the Marshal engine.

These are deliberately stdlib-only (dataclasses + enums) so the engine has no heavy
dependencies and the types are trivially serializable for fleet state and usage logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class PermissionMode(str, Enum):
    """Normalized permission tiers, mapped to each backend's native flags by the adapter.

    Headless agents have no stdin, so a prompting mode would deadlock. SAFE_EDIT is the
    default and must never prompt: it writes inside the worktree without confirmation.
    """

    READ_ONLY = "read-only"   # plan/inspect only; no edits, no shell mutations
    SAFE_EDIT = "safe-edit"   # edit + run within the worktree, no prompts (default)
    YOLO = "yolo"             # fully unrestricted; opt-in only


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class UsageSource(str, Enum):
    """Provenance of a usage record — never present an estimate as ground truth."""

    NATIVE = "native"            # backend reported tokens+cost in its output
    ADMIN_API = "admin-api"      # fetched from a provider account/admin API
    ESTIMATED = "estimated"      # computed from tokens via a local price table
    SCRAPED = "scraped"          # parsed off terminal output (least trustworthy)
    UNAVAILABLE = "unavailable"  # backend exposes no usage data


@dataclass(frozen=True)
class Capabilities:
    """Feature flags so the orchestrator degrades gracefully per backend."""

    json_output: bool = False
    stream_json: bool = False
    sessions: bool = False        # resume/continue support
    server_mode: bool = False     # e.g. `opencode serve`
    native_usage: bool = False    # emits tokens/cost in its own output
    permission_modes: frozenset[PermissionMode] = frozenset()


@dataclass
class TaskSpec:
    """A single unit of work handed to one agent."""

    id: str
    goal: str                              # natural-language task for the agent
    role: str | None = None                # routing role (planner/coder/writer/reviewer/...);
                                           # policy maps role -> backend. Engine stays mechanism.
    context_files: list[str] = field(default_factory=list)  # minimal files the worker should see
    files_touched: list[str] = field(default_factory=list)  # declared scope -> conflict analysis
    base_branch: str | None = None         # branch to base the worktree on (None = current HEAD)


@dataclass
class RunOpts:
    """How to run a TaskSpec. Backend-agnostic; adapters translate these to native flags."""

    cwd: Path                              # where the agent runs (typically a worktree)
    permission: PermissionMode = PermissionMode.SAFE_EDIT
    model: str | None = None
    session_id: str | None = None         # resume a prior session if the backend supports it
    timeout_s: int = 600                  # external timeout + kill — never run without one
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class UsageRecord:
    """Normalized usage/cost for one run."""

    backend: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    source: UsageSource = UsageSource.UNAVAILABLE


@dataclass
class AgentResult:
    """Normalized result of one agent run, regardless of backend."""

    status: RunStatus
    text: str = ""                         # final assistant message
    session_id: str | None = None
    usage: UsageRecord | None = None
    files_changed: list[str] = field(default_factory=list)
    exit_code: int | None = None
    duration_ms: int = 0                    # wall-clock around the run, stamped by base.run()
    error: str | None = None
    raw_stdout: str = ""
    raw_stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.SUCCEEDED
