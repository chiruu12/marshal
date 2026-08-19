"""Resolving a GitHub PR to the commit range a review team can read.

A pull request *is* a commit range; the only thing Marshal lacks is a way to find its endpoints.
So this module does exactly that and nothing else - it asks ``gh`` for the PR's base branch and head
commit, makes both reachable locally, and hands back refs. There is no ``pr`` target kind: the panel
still reviews a ``range``, which means every existing ``target: range`` team works on a PR unchanged.

**Why this lives in `interfaces` and not the engine.** `orchestration/teams.py` must not learn that
GitHub exists - it takes a base and a head and stays a pure git operation, which is what keeps the
engine embeddable and `gh`-free for anyone using Marshal on a repo that was never on GitHub. The
dependency stops here, at the surface a driver actually calls.

**The head is used as a SHA, never as a branch name.** A PR can come from a fork whose branch the
author chose, and that name reaches us as data: a branch called ``--output=...`` handed to git as a
revision is an arbitrary file write. `gh` gives us ``headRefOid``, a 40-hex commit id, so the
attacker-controlled string never becomes an argument.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import ConfigError
from ..runtime.env import DETACHED_STDIO

#: A git ref name we are willing to pass to git as a revision. Deliberately narrower than git's own
#: rules: this is the *base* branch of a PR, which in practice is `main`/`master`/`release/x`, and a
#: tight allowlist is cheaper to reason about than enumerating what git would mis-parse.
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")

_SHA = re.compile(r"^[0-9a-f]{7,64}$")

#: Seconds for any single `gh`/`git` call here. These talk to the network, and a driver waiting on a
#: review panel should get a clear failure rather than a hang - the same rule agent runs follow.
NETWORK_TIMEOUT_S = 60.0

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class PullRequestRef:
    """A PR reduced to what a `range` review needs, plus what a human needs to confirm it is right."""

    number: int
    base: str
    head: str
    title: str
    url: str
    state: str
    #: True when the PR is closed or merged. Not an error - reviewing a merged PR is legitimate -
    #: but the caller should say so, since a stale review reads exactly like a current one.
    stale: bool


def _run(argv: Sequence[str], cwd: Path, runner: Runner) -> subprocess.CompletedProcess[str]:
    return runner(
        list(argv),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        # Closed stdin + a hard timeout, for the same reason every other subprocess here has them:
        # `gh` prompts for auth and `git fetch` prompts for credentials, and a prompt with no stdin
        # hangs forever. GIT_TERMINAL_PROMPT=0 turns the credential prompt into an error instead.
        # The setsid half of DETACHED_STDIO matters here too: `gh` is terminal-aware and will reach
        # for one if it inherits it.
        **DETACHED_STDIO,
        env={"GIT_TERMINAL_PROMPT": "0", **_inherit_env()},
        timeout=NETWORK_TIMEOUT_S,
    )


def _inherit_env() -> dict[str, str]:
    import os

    return dict(os.environ)


def resolve_pr(
    repo: Path,
    number: int,
    *,
    remote: str = "origin",
    runner: Runner = subprocess.run,
) -> PullRequestRef:
    """Look up PR ``number`` and make its base and head reachable in ``repo``.

    Returns refs suitable for ``diff_range``, which uses ``base...head`` - three-dot, so the diff is
    already taken from the merge base. That matters: reviewing ``base..head`` (two-dot) against a
    moving branch would show the panel everything ``main`` gained since the PR was opened, and the
    reviewers would spend their lenses on code the PR's author never wrote.

    Raises `ConfigError` with an actionable message when `gh` is missing, unauthenticated, or the PR
    does not exist - a review panel is expensive, so this fails before any reviewer spawns.
    """
    if number <= 0:
        raise ConfigError(f"invalid PR number {number}: must be a positive integer")
    if shutil.which("gh") is None:
        raise ConfigError(
            "reviewing a PR needs the GitHub CLI (`gh`) on PATH, and it was not found. "
            "Install it, or pass `base`/`head` refs directly to review the range by hand."
        )

    meta = _fetch_metadata(repo, number, runner)
    base_name = str(meta.get("baseRefName") or "")
    head_oid = str(meta.get("headRefOid") or "")

    if not _SAFE_REF.match(base_name):
        raise ConfigError(
            f"PR #{number} has an unusable base branch {base_name!r}; review it by hand with "
            "explicit base/head refs"
        )
    if not _SHA.match(head_oid):
        # Without a usable commit id we would have to fall back to the head BRANCH name, which is
        # attacker-chosen on a fork. Refusing is the safe direction.
        raise ConfigError(
            f"PR #{number} did not report a head commit id; cannot review it safely"
        )

    # Fetch the head COMMIT, not a branch: a fork's head is not in this repo, and pull/N/head is the
    # server's own copy of it, so this works for forks and for a deleted source branch alike.
    #
    # Landed on a ref of our own rather than read back out of FETCH_HEAD, which is a single
    # repo-global file: one MCP server resolves PRs for a workspace concurrently, so two overlapping
    # resolutions would overwrite each other's FETCH_HEAD and make a perfectly valid review fail.
    # The ref is namespaced per PR, so concurrent resolutions cannot collide at all.
    head_ref = f"refs/marshal/pr/{number}/head"
    _fetch(repo, remote, f"+refs/pull/{number}/head:{head_ref}", number, runner)

    # The head OID came from `gh` BEFORE the fetch, so a force-push in between leaves us naming a
    # commit that is no longer the PR's head. Asking merely whether that object EXISTS locally does
    # not catch it: the superseded commit is usually still lying around from an earlier fetch, and
    # the panel would then review the old revision as though it were current. So compare against
    # what this fetch actually retrieved - `gh`'s answer was a guess by the time we used it.
    fetched = _rev_parse(repo, head_ref, runner)
    if fetched != head_oid:
        raise ConfigError(
            f"PR #{number} moved while it was being resolved: `gh` reported head "
            f"{head_oid[:12]} but the fetch retrieved {(fetched or 'nothing')[:12]} (it was most "
            "likely force-pushed). Retry."
        )

    # The base must also be current, or the merge base is computed against a stale copy and the
    # panel reviews a range that never existed. Fetched with an EXPLICIT refspec rather than the
    # bare branch name: a bare `git fetch origin main` only updates `origin/main` if the remote's
    # configured fetch mapping happens to cover it, and a repo cloned with `--single-branch`, or one
    # whose `remote.origin.fetch` was narrowed by hand, would report success while leaving the
    # remote-tracking ref exactly as stale as before. The forced explicit mapping does not depend on
    # that config being conventional.
    base_ref = f"refs/remotes/{remote}/{base_name}"
    _fetch(repo, remote, f"+refs/heads/{base_name}:{base_ref}", number, runner)
    if _rev_parse(repo, f"{base_ref}^{{commit}}", runner) is None:
        raise ConfigError(
            f"base branch {base_name!r} of PR #{number} is not a commit after fetching it; "
            "review the range by hand with explicit base/head refs"
        )
    state = str(meta.get("state") or "").upper()
    return PullRequestRef(
        number=number,
        base=base_ref,
        head=head_oid,
        title=str(meta.get("title") or ""),
        url=str(meta.get("url") or ""),
        state=state,
        stale=state in {"CLOSED", "MERGED"},
    )


def _fetch_metadata(repo: Path, number: int, runner: Runner) -> dict[str, Any]:
    proc = _run(
        [
            "gh", "pr", "view", str(number),
            "--json", "baseRefName,headRefOid,title,url,state",
        ],
        repo,
        runner,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        hint = ""
        lowered = detail.lower()
        if "auth" in lowered or "login" in lowered:
            hint = " Run `gh auth login`."
        elif "could not resolve" in lowered or "not found" in lowered:
            hint = f" Check that #{number} exists in this repository's remote."
        raise ConfigError(f"cannot read PR #{number}: {detail or 'gh failed'}.{hint}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"cannot read PR #{number}: gh returned unparseable JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"cannot read PR #{number}: gh returned {type(data).__name__}, not an object")
    return data


def _fetch(repo: Path, remote: str, refspec: str, number: int, runner: Runner) -> None:
    proc = _run(["git", "fetch", "--quiet", remote, refspec], repo, runner)
    if proc.returncode != 0:
        raise ConfigError(
            f"cannot fetch PR #{number} ({refspec} from {remote}): "
            f"{(proc.stderr or '').strip() or 'git fetch failed'}"
        )


def _rev_parse(repo: Path, rev: str, runner: Runner) -> str | None:
    """The object id ``rev`` names, or None if it does not resolve."""
    probe = _run(["git", "rev-parse", "--verify", "--quiet", rev], repo, runner)
    if probe.returncode != 0:
        return None
    return (probe.stdout or "").strip() or None
