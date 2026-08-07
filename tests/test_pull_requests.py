"""`resolve_pr` - turning a GitHub PR into the commit range a review team reads.

No network and no real `gh`: the subprocess runner is injected, so each test states exactly what
the tools returned and asserts on the argv that was built from it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from marshal_engine.core.config import ConfigError
from marshal_engine.interfaces.pull_requests import resolve_pr

_OID = "a" * 40


class _Runner:
    """Records every argv and replies from a scripted table keyed on the command shape."""

    def __init__(self, replies: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.replies = replies or {}

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        for key, (code, out) in self.replies.items():
            if key in " ".join(argv):
                return subprocess.CompletedProcess(argv, code, out, "" if code == 0 else out)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def argv_for(self, needle: str) -> list[str] | None:
        return next((c for c in self.calls if needle in " ".join(c)), None)


def _meta(**over: Any) -> str:
    import json

    base = {
        "baseRefName": "main",
        "headRefOid": _OID,
        "title": "Add a thing",
        "url": "https://github.com/o/r/pull/7",
        "state": "OPEN",
    }
    base.update(over)
    return json.dumps(base)


@pytest.fixture(autouse=True)
def _gh_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "marshal_engine.interfaces.pull_requests.shutil.which", lambda _: "/usr/bin/gh"
    )


def test_resolves_a_pr_to_remote_base_and_head_sha(tmp_path: Path) -> None:
    runner = _Runner({"gh pr view": (0, _meta())})
    ref = resolve_pr(tmp_path, 7, runner=runner)

    assert ref.base == "origin/main"
    assert ref.head == _OID
    assert ref.number == 7
    assert ref.title == "Add a thing"
    assert ref.stale is False


def test_the_head_is_the_sha_never_the_branch_name(tmp_path: Path) -> None:
    """A fork's branch name is attacker-chosen and reaches us as data.

    `diff_range` refuses a ref starting with '-', but the durable fix is never to put the name in
    argv at all: `--output=<path>` as a "revision" turns a read-only diff into a file write. Using
    `headRefOid` means the hostile string is never an argument.
    """
    runner = _Runner({"gh pr view": (0, _meta(headRefName="--output=/tmp/pwned"))})
    ref = resolve_pr(tmp_path, 7, runner=runner)

    assert ref.head == _OID
    for call in runner.calls:
        assert "--output=/tmp/pwned" not in " ".join(call)


def test_fetches_the_pr_head_by_pull_ref_so_forks_work(tmp_path: Path) -> None:
    """A fork's head is not in this repo; `pull/N/head` is the server's own copy of it."""
    runner = _Runner({"gh pr view": (0, _meta())})
    resolve_pr(tmp_path, 7, runner=runner)

    fetch = runner.argv_for("pull/7/head")
    assert fetch is not None
    assert fetch[:3] == ["git", "fetch", "--quiet"]
    assert fetch[3] == "origin"


def test_prefers_the_remote_tracking_base_over_a_stale_local_branch(tmp_path: Path) -> None:
    """Diffing against a stale local `main` silently widens the review past the PR's own commits."""
    runner = _Runner({"gh pr view": (0, _meta())})
    ref = resolve_pr(tmp_path, 7, runner=runner)
    assert ref.base == "origin/main"


def test_falls_back_to_the_local_base_when_no_remote_tracking_ref_exists(tmp_path: Path) -> None:
    runner = _Runner({
        "gh pr view": (0, _meta()),
        "rev-parse --verify --quiet origin/main": (1, ""),
    })
    ref = resolve_pr(tmp_path, 7, runner=runner)
    assert ref.base == "main"


def test_refuses_when_neither_base_ref_is_present(tmp_path: Path) -> None:
    runner = _Runner({
        "gh pr view": (0, _meta()),
        "rev-parse": (1, ""),
    })
    with pytest.raises(ConfigError, match="not present locally"):
        resolve_pr(tmp_path, 7, runner=runner)


def test_refuses_an_unsafe_base_branch_name(tmp_path: Path) -> None:
    runner = _Runner({"gh pr view": (0, _meta(baseRefName="--upload-pack=evil"))})
    with pytest.raises(ConfigError, match="unusable base branch"):
        resolve_pr(tmp_path, 7, runner=runner)


def test_refuses_a_missing_head_commit_id_rather_than_using_the_branch(tmp_path: Path) -> None:
    """Without a commit id the only fallback is the fork-controlled branch name. Refuse instead."""
    runner = _Runner({"gh pr view": (0, _meta(headRefOid=""))})
    with pytest.raises(ConfigError, match="head commit id"):
        resolve_pr(tmp_path, 7, runner=runner)


def test_a_merged_pr_is_resolvable_but_flagged_stale(tmp_path: Path) -> None:
    """Reviewing a merged PR is legitimate; a stale review just must not read as a current one."""
    runner = _Runner({"gh pr view": (0, _meta(state="MERGED"))})
    ref = resolve_pr(tmp_path, 7, runner=runner)
    assert ref.stale is True
    assert ref.state == "MERGED"


def test_missing_gh_is_an_actionable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "marshal_engine.interfaces.pull_requests.shutil.which", lambda _: None
    )
    with pytest.raises(ConfigError, match="GitHub CLI"):
        resolve_pr(tmp_path, 7, runner=_Runner())


def test_an_auth_failure_names_the_fix(tmp_path: Path) -> None:
    runner = _Runner({"gh pr view": (1, "gh: To get started with GitHub CLI, please run: gh auth login")})
    with pytest.raises(ConfigError, match="gh auth login"):
        resolve_pr(tmp_path, 7, runner=runner)


def test_an_unknown_pr_names_the_fix(tmp_path: Path) -> None:
    runner = _Runner({"gh pr view": (1, "could not resolve to a PullRequest")})
    with pytest.raises(ConfigError, match="exists in this repository"):
        resolve_pr(tmp_path, 7, runner=runner)


def test_unparseable_gh_output_is_refused(tmp_path: Path) -> None:
    runner = _Runner({"gh pr view": (0, "not json at all")})
    with pytest.raises(ConfigError, match="unparseable JSON"):
        resolve_pr(tmp_path, 7, runner=runner)


@pytest.mark.parametrize("number", [0, -1])
def test_a_nonpositive_pr_number_is_refused_before_any_subprocess(
    tmp_path: Path, number: int
) -> None:
    runner = _Runner()
    with pytest.raises(ConfigError, match="positive integer"):
        resolve_pr(tmp_path, number, runner=runner)
    assert runner.calls == []


def test_every_subprocess_runs_with_stdin_closed_and_a_timeout(tmp_path: Path) -> None:
    """`gh` prompts for auth and `git fetch` for credentials; a prompt with no stdin hangs forever.

    Same headless rule the agent runs follow - the whole point is that a driver waiting on a review
    panel gets a clear failure instead of a hang.
    """
    seen: list[dict[str, Any]] = []

    def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kw)
        out = _meta() if "gh" in argv[0] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    resolve_pr(tmp_path, 7, runner=runner)

    assert seen
    for kw in seen:
        assert kw["stdin"] is subprocess.DEVNULL
        assert kw["timeout"] > 0
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
