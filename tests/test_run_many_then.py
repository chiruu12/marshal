"""run_many per-job ``then`` follow-up chains (issue #103)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from marshal_engine import AgentResult, Capabilities, RunOpts, RunStatus, TaskSpec
from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.config import ClientConfig, FleetConfig, PermissionMode
from marshal_engine.fleet import Fleet, RunManyJob, RunRequest
from marshal_engine.service import MarshalService


class _Writer(CodingAgentBackend):
    name = "writer"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "open('out.txt','w').write('hi'); print('done')"]

    def map_permission(self, mode):  # noqa: ANN001
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _Exploder(CodingAgentBackend):
    name = "boom"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "raise SystemExit('kaboom')"]

    def map_permission(self, mode):  # noqa: ANN001
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.FAILED, error="kaboom", exit_code=exit_code or 1)


class _OrderMarker(CodingAgentBackend):
    """Append ``<id>`` at start and ``<id>:end`` after an optional sleep to a shared log."""

    name = "marker"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self, log_path: Path) -> None:
        self._log = log_path

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        delay = float(task.goal)
        # Flush after each write so concurrent chains see durable ordering.
        script = (
            f"p=open({str(self._log)!r},'a');"
            f"p.write({task.id!r}+'\\n');p.flush();"
            f"import time;time.sleep({delay});"
            f"p.write({task.id!r}+':end\\n');p.flush();"
            f"print('ok')"
        )
        return [sys.executable, "-c", script]

    def map_permission(self, mode):  # noqa: ANN001
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _Reader(CodingAgentBackend):
    """Fails unless out.txt from a chained primary is present."""

    name = "reader"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [
            sys.executable,
            "-c",
            "import pathlib; assert pathlib.Path('out.txt').read_text() == 'hi'; print('saw work')",
        ]

    def map_permission(self, mode):  # noqa: ANN001
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _TextOnly(CodingAgentBackend):
    """Exits clean with prose only — no file changes (exited_clean, not empty)."""

    name = "talker"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('here is my analysis; no edits')"]

    def map_permission(self, mode):  # noqa: ANN001
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _CountingReader(CodingAgentBackend):
    """Follow-up stub that records how many times it was invoked."""

    name = "counter"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self) -> None:
        self.invocations = 0

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        self.invocations += 1
        return [sys.executable, "-c", "print('reviewed')"]

    def map_permission(self, mode):  # noqa: ANN001
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _SelfCommitter(CodingAgentBackend):
    """Writes a file and commits it inside the worktree (agent self-commit → clean tree)."""

    name = "selfcommit"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        script = (
            "import subprocess;"
            "open('out.txt','w').write('hi');"
            "subprocess.run(['git','add','out.txt'], check=True);"
            "subprocess.run(['git','commit','-m','agent work'], check=True);"
            "print('done')"
        )
        return [sys.executable, "-c", script]

    def map_permission(self, mode):  # noqa: ANN001
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _init_repo(r)
    return r


def _svc(repo: Path, backends: dict[str, CodingAgentBackend]) -> MarshalService:
    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(name="worker", backend="writer", permission=PermissionMode.SAFE_EDIT),
            "reviewer": ClientConfig(name="reviewer", backend="reader", permission=PermissionMode.SAFE_EDIT),
        }
    )
    return MarshalService(repo, cfg, backends=backends)


def test_then_bad_spec_fails_fast_before_any_run(repo: Path) -> None:
    svc = _svc(repo, {"writer": _Writer(), "reader": _Reader()})
    with pytest.raises(ValueError, match="no such client"):
        svc.run_many(
            [
                {
                    "client": "worker",
                    "goal": "build",
                    "task_id": "j1",
                    "then": {"client": "nope", "goal": "review"},
                }
            ]
        )
    assert svc.status() == []


def test_then_skipped_when_primary_failed(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer(), "boom": _Exploder(), "reader": _Reader()})
    results = fleet.run_many(
        [
            RunManyJob(
                request=RunRequest(backend_name="boom", task=TaskSpec(id="bad", goal="x")),
                then=RunRequest(backend_name="reader", task=TaskSpec(id="bad-then", goal="review")),
            )
        ],
        stagger_s=0,
    )
    assert len(results) == 1
    assert results[0].primary.status == "failed"
    assert results[0].then is None
    assert results[0].then_skipped and "did not succeed" in results[0].then_skipped


def test_then_skipped_when_primary_exits_clean_with_no_diff(repo: Path) -> None:
    """Text-only primary is exited_clean but has nothing to review — do not spawn ``then``."""
    counter = _CountingReader()
    fleet = Fleet(repo, {"talker": _TextOnly(), "counter": counter})
    results = fleet.run_many(
        [
            RunManyJob(
                request=RunRequest(backend_name="talker", task=TaskSpec(id="chat", goal="x")),
                then=RunRequest(
                    backend_name="counter",
                    task=TaskSpec(id="chat-then", goal="review"),
                ),
            )
        ],
        stagger_s=0,
    )
    assert results[0].primary.status == "exited_clean"
    assert results[0].then is None
    assert results[0].then_skipped is not None
    assert "no diff" in results[0].then_skipped
    assert counter.invocations == 0


def test_then_runs_when_primary_self_committed(repo: Path) -> None:
    """Self-committed primary leaves a clean tree; follow-up must still run and see the work."""

    class _SeeingCounter(_CountingReader):
        name = "seer"

        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            self.invocations += 1
            return [
                sys.executable,
                "-c",
                "import pathlib; assert pathlib.Path('out.txt').read_text() == 'hi'; print('saw work')",
            ]

    seer = _SeeingCounter()
    fleet = Fleet(repo, {"selfcommit": _SelfCommitter(), "seer": seer})
    results = fleet.run_many(
        [
            RunManyJob(
                request=RunRequest(
                    backend_name="selfcommit",
                    task=TaskSpec(id="build", goal="x"),
                ),
                then=RunRequest(
                    backend_name="seer",
                    task=TaskSpec(id="review", goal="review"),
                ),
            )
        ],
        stagger_s=0,
    )
    assert results[0].primary.status == "exited_clean"
    assert results[0].then is not None
    assert results[0].then.status == "exited_clean"
    assert results[0].then_skipped is None
    assert seer.invocations >= 1  # build_invocation may be probed more than once


def test_then_runs_against_committed_primary_work(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer(), "reader": _Reader()})
    results = fleet.run_many(
        [
            RunManyJob(
                request=RunRequest(backend_name="writer", task=TaskSpec(id="build", goal="x")),
                then=RunRequest(backend_name="reader", task=TaskSpec(id="review", goal="review")),
            )
        ],
        stagger_s=0,
    )
    assert results[0].primary.status == "exited_clean"
    assert results[0].then is not None
    assert results[0].then.status == "exited_clean"
    assert results[0].then_skipped is None


def test_then_failure_does_not_abort_batch(repo: Path) -> None:
    fleet = Fleet(
        repo,
        {"writer": _Writer(), "boom": _Exploder(), "reader": _Reader()},
    )
    results = fleet.run_many(
        [
            RunManyJob(
                request=RunRequest(backend_name="writer", task=TaskSpec(id="ok", goal="x")),
                then=RunRequest(backend_name="boom", task=TaskSpec(id="ok-then", goal="review")),
            ),
            RunManyJob(request=RunRequest(backend_name="writer", task=TaskSpec(id="solo", goal="y"))),
        ],
        max_concurrency=2,
        stagger_s=0,
    )
    assert results[0].primary.status == "exited_clean"
    assert results[0].then is not None and results[0].then.status == "failed"
    assert results[1].primary.status == "exited_clean"


def test_then_starts_before_slow_sibling_primary_finishes(repo: Path) -> None:
    """The fast job's follow-up must land before the slow sibling primary finishes.

    A pure barrier (all primaries, then all follow-ups) would put ``fast_then`` after
    ``slow_start:end``; pipelined chains put it before. Sleeps leave CI margin but keep
    that ordering impossible under a barrier.
    """
    log = repo / "order.log"
    fleet = Fleet(repo, {"writer": _Writer(), "marker": _OrderMarker(log)})
    results = fleet.run_many(
        [
            RunManyJob(
                request=RunRequest(backend_name="writer", task=TaskSpec(id="fast", goal="x")),
                then=RunRequest(
                    backend_name="marker",
                    task=TaskSpec(id="fast_then", goal="0.05"),
                ),
            ),
            RunManyJob(
                request=RunRequest(
                    backend_name="marker",
                    task=TaskSpec(id="slow_start", goal="1.2"),
                )
            ),
        ],
        max_concurrency=2,
        stagger_s=0,
    )
    assert results[0].then is not None and results[0].then.status == "exited_clean"
    assert results[1].primary.status == "exited_clean"
    lines = log.read_text().splitlines()
    assert "slow_start" in lines and "slow_start:end" in lines and "fast_then" in lines
    # Impossible under a barrier: follow-up starts only after every primary (incl. slow) ends.
    assert lines.index("fast_then") < lines.index("slow_start:end")


def test_service_run_many_then_via_job_dict(repo: Path) -> None:
    svc = _svc(repo, {"writer": _Writer(), "reader": _Reader()})
    results = svc.run_many(
        [
            {
                "client": "worker",
                "goal": "build",
                "task_id": "svc1",
                "then": {"client": "reviewer", "goal": "review", "task_id": "svc1-review"},
            }
        ]
    )
    assert results[0].primary.task_id == "svc1"
    assert results[0].then is not None
    assert results[0].then.task_id == "svc1-review"
