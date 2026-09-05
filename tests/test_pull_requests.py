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
        joined = " ".join(argv)
        for key, (code, out) in self.replies.items():
            if key in joined:
                return subprocess.CompletedProcess(argv, code, out, "" if code == 0 else out)
        # Default: the happy path. `rev-parse` must answer with an OID, and FETCH_HEAD must agree
        # with the head `gh` reported, or every test would look like a force-push race.
        if "rev-parse" in joined:
            return subprocess.CompletedProcess(argv, 0, f"{_OID}\n", "")
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


def test_a_hanging_gh_becomes_a_config_error_not_a_traceback(tmp_path: Path) -> None:
    """REGRESSION (P2): the timeout exists so a prompting `gh` or `git fetch` fails FAST; the raw
    `TimeoutExpired` escaping it instead made it fail LOUDLY - a traceback out of the CLI, which
    catches only ConfigError, and an unclassified error over MCP."""

    def hangs(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 60.0)

    with pytest.raises(ConfigError) as exc:
        resolve_pr(tmp_path, 7, runner=hangs)
    msg = str(exc.value)
    assert "timed out" in msg and "60.0s" in msg
    assert "Traceback" not in msg


def test_pr_metadata_is_read_from_the_same_remote_the_refs_are_fetched_from(
    tmp_path: Path,
) -> None:
    """REGRESSION (P2): `gh pr view` ran with no `--repo`, so it resolved the number against gh's
    own configured default - on a fork clone usually the upstream - while the refs were always
    fetched from `origin`. The two then disagreed about which repository the PR belongs to, and
    the failure read as "the PR does not exist"."""
    runner = _Runner({
        "gh pr view": (0, _meta()),
        "git remote get-url": (0, "git@github.com:me/my-fork.git\n"),
    })
    resolve_pr(tmp_path, 7, runner=runner)
    argv = runner.argv_for("gh pr view")
    assert argv is not None and "--repo" in argv
    assert argv[argv.index("--repo") + 1] == "me/my-fork"


def test_pr_metadata_falls_back_when_the_remote_is_not_a_github_url(tmp_path: Path) -> None:
    """Anti-blanket control: an unparseable remote must not become a bogus `--repo` - gh's own
    default is the right answer there, not a guess."""
    runner = _Runner({
        "gh pr view": (0, _meta()),
        "git remote get-url": (0, "/srv/mirrors/proj.git\n"),
    })
    resolve_pr(tmp_path, 7, runner=runner)
    argv = runner.argv_for("gh pr view")
    assert argv is not None and "--repo" not in argv


def test_resolves_a_pr_to_remote_base_and_head_sha(tmp_path: Path) -> None:
    runner = _Runner({"gh pr view": (0, _meta())})
    ref = resolve_pr(tmp_path, 7, runner=runner)

    assert ref.base == "refs/remotes/origin/main"
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


def test_the_base_is_the_remote_tracking_ref_never_an_ambiguous_short_name(tmp_path: Path) -> None:
    """A stale local `main` would silently widen the review past the PR's own commits.

    Fully qualified, so the name cannot resolve to anything else: `origin/main` is also a legal
    LOCAL branch name, and git would prefer that branch over the remote-tracking ref.
    """
    runner = _Runner({"gh pr view": (0, _meta())})
    ref = resolve_pr(tmp_path, 7, runner=runner)
    assert ref.base == "refs/remotes/origin/main"


def test_the_base_is_fetched_with_an_explicit_forced_refspec(tmp_path: Path) -> None:
    """A bare `git fetch origin main` updates the remote-tracking ref only if the remote's fetch
    mapping happens to cover it.

    A `--single-branch` clone, or a hand-narrowed `remote.origin.fetch`, would let that fetch
    SUCCEED while leaving `origin/main` exactly as stale as before - a silent wrong diff, which is
    the failure mode this whole resolver exists to prevent. The explicit mapping does not depend on
    the config being conventional.
    """
    runner = _Runner({"gh pr view": (0, _meta())})
    resolve_pr(tmp_path, 7, runner=runner)

    argv = runner.argv_for("refs/heads/main")
    assert argv is not None
    assert argv[-1] == "+refs/heads/main:refs/remotes/origin/main"


def test_refuses_when_the_fetched_base_does_not_resolve_to_a_commit(tmp_path: Path) -> None:
    runner = _Runner({
        "gh pr view": (0, _meta()),
        "refs/remotes/origin/main^{commit}": (1, ""),
    })
    with pytest.raises(ConfigError, match="not a commit"):
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
        out = _meta() if "gh" in argv[0] else (_OID if "rev-parse" in argv else "")
        return subprocess.CompletedProcess(argv, 0, out, "")

    resolve_pr(tmp_path, 7, runner=runner)

    assert seen
    for kw in seen:
        assert kw["stdin"] is subprocess.DEVNULL
        assert kw["timeout"] > 0
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_a_base_that_could_not_be_refreshed_is_refused_not_reviewed(tmp_path: Path) -> None:
    """A stale base produces a plausible, wrong diff - and nothing in the output says so.

    `origin/main` from whenever the driver last fetched still resolves, so tolerating the failed
    refresh would hand the panel a range the PR never had: widened with commits `main` gained
    since, or narrowed by ones it lost. Every reviewer would then spend its lens on the wrong code
    and report confidently. There is no degraded answer worth preferring to an error here.
    """
    runner = _Runner({
        "gh pr view": (0, _meta()),
        "refs/heads/main": (1, "network is unreachable"),
    })

    with pytest.raises(ConfigError) as exc:
        resolve_pr(tmp_path, 7, runner=runner)

    assert "network is unreachable" in str(exc.value)
    # It failed at the fetch, so no base ref was ever probed, let alone handed back.
    assert runner.argv_for("refs/remotes/origin/main^{commit}") is None


def test_a_force_push_is_caught_even_though_the_old_commit_is_still_local(tmp_path: Path) -> None:
    """The dangerous force-push is the one where the superseded commit IS still present.

    An earlier fetch of this PR leaves the old head in the object store, so "does this object
    exist?" answers yes and the panel reviews the revision the author already replaced - a stale
    review that reads exactly like a current one. Only comparing against what THIS fetch retrieved
    catches it, so the check is an equality against the fetched ref, not an existence probe.
    """
    newer = "b" * 40
    runner = _Runner({
        "gh pr view": (0, _meta()),
        "rev-parse --verify --quiet refs/marshal/pr/7/head": (0, newer),
    })

    with pytest.raises(ConfigError) as exc:
        resolve_pr(tmp_path, 7, runner=runner)

    message = str(exc.value)
    assert "moved" in message
    assert _OID[:12] in message and newer[:12] in message


def test_a_head_the_fetch_did_not_retrieve_at_all_is_refused(tmp_path: Path) -> None:
    runner = _Runner({
        "gh pr view": (0, _meta()),
        "rev-parse --verify --quiet refs/marshal/pr/7/head": (1, ""),
    })

    with pytest.raises(ConfigError, match="moved"):
        resolve_pr(tmp_path, 7, runner=runner)


def test_the_head_is_checked_against_the_fetched_ref_after_the_fetch_not_before(tmp_path: Path) -> None:
    """Ordering is the whole point: reading the ref first would report the PREVIOUS fetch's head."""
    runner = _Runner({"gh pr view": (0, _meta())})
    resolve_pr(tmp_path, 7, runner=runner)

    joined = [" ".join(c) for c in runner.calls]
    fetched = next(i for i, c in enumerate(joined) if "pull/7/head" in c)
    probed = next(i for i, c in enumerate(joined) if "rev-parse" in c and "pr/7/head" in c)
    assert probed > fetched


def test_the_head_lands_on_a_per_pr_ref_so_concurrent_resolutions_cannot_collide(
    tmp_path: Path,
) -> None:
    """FETCH_HEAD is one repo-global file; a workspace resolves PRs concurrently.

    Two overlapping `run_team(pr=...)` calls would overwrite each other's FETCH_HEAD, and the
    force-push check would then reject a perfectly current head - failing a valid review rather
    than a stale one. A ref namespaced by PR number has no shared state to race over.
    """
    runner = _Runner({"gh pr view": (0, _meta())})
    resolve_pr(tmp_path, 7, runner=runner)

    fetch = runner.argv_for("refs/pull/7/head")
    assert fetch is not None
    assert fetch[-1] == "+refs/pull/7/head:refs/marshal/pr/7/head"
    assert runner.argv_for("FETCH_HEAD") is None
