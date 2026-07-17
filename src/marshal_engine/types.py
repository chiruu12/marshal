"""Core data types shared across the Marshal engine.

Value objects are Pydantic models so construction validates inputs and (de)serialization to fleet
state / usage logs / the MCP surface is uniform. Enums stay plain ``str`` enums (Pydantic handles
them natively). The loose, version-variable JSON that backend CLIs emit is deliberately parsed as
plain dicts in the adapters - strict models there would reject on an unexpected upstream field.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict


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
    EMPTY = "empty"           # exited clean but produced no work; counts in $/run, not $/succeeded
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    # The agent finished and produced work, but the workspace's `verify:` command rejected it.
    # Distinct from FAILED so a driver can tell "the run broke" from "reviewable work exists but
    # the repo's gate said no" - the worktree is kept for review either way.
    VERIFY_FAILED = "verify_failed"


class UsageSource(str, Enum):
    """Provenance of a usage record - never present an estimate as ground truth."""

    NATIVE = "native"            # backend reported tokens+cost in its output
    ADMIN_API = "admin-api"      # fetched from a provider account/admin API
    ESTIMATED = "estimated"      # computed from tokens via a local price table
    SCRAPED = "scraped"          # parsed off terminal output (least trustworthy)
    UNAVAILABLE = "unavailable"  # backend exposes no usage data


class Capabilities(BaseModel):
    """Feature flags so the orchestrator degrades gracefully per backend."""

    model_config = ConfigDict(frozen=True)

    json_output: bool = False
    stream_json: bool = False
    sessions: bool = False        # resume/continue support
    server_mode: bool = False     # e.g. `opencode serve`
    native_usage: bool = False    # emits tokens/cost in its own output
    permission_modes: frozenset[PermissionMode] = frozenset()


class TaskSpec(BaseModel):
    """A single unit of work handed to one agent."""

    id: str
    goal: str                              # natural-language task for the agent
    role: str | None = None                # routing role (planner/coder/writer/reviewer/...);
                                           # policy maps role -> backend. Engine stays mechanism.
    context_files: list[str] = []          # minimal files the worker should see
    files_touched: list[str] = []          # declared scope -> conflict analysis
    base_branch: str | None = None         # branch to base the worktree on (None = current HEAD)


class RunOpts(BaseModel):
    """How to run a TaskSpec. Backend-agnostic; adapters translate these to native flags."""

    cwd: Path                              # where the agent runs (typically a worktree)
    permission: PermissionMode = PermissionMode.SAFE_EDIT
    model: str | None = None
    session_id: str | None = None         # resume a prior session if the backend supports it
    timeout_s: int = 600                  # external timeout + kill - never run without one
    extra_env: dict[str, str] = {}
    on_pid: Callable[[int], None] | None = None  # called by base.run() with the child pid


class UsageRecord(BaseModel):
    """Normalized usage/cost for one run."""

    backend: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    source: UsageSource = UsageSource.UNAVAILABLE


class AgentResult(BaseModel):
    """Normalized result of one agent run, regardless of backend."""

    status: RunStatus
    text: str = ""                         # final assistant message
    session_id: str | None = None
    usage: UsageRecord | None = None
    files_changed: list[str] = []
    exit_code: int | None = None
    duration_ms: int = 0                    # wall-clock around the run, stamped by base.run()
    error: str | None = None
    raw_stdout: str = ""
    raw_stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.SUCCEEDED
