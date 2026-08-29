"""Unit tests for provisioning fail-closed guards (hardlinks, casefold, dest TOCTOU)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from marshal_engine.orchestration import provisioning as provisioning_mod
from marshal_engine.orchestration.provisioning import (
    ARTIFACT_DIR,
    _is_refused_read_path,
    _make_readonly,
    _provision_read_paths,
    _validate_read_path_tree,
    harvest_artifacts,
)
from marshal_engine.runtime.worktree import Worktree


def _git_init(root: Path) -> None:
    for args in (("init", "-q"), ("config", "user.email", "t@x"), ("config", "user.name", "t")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _worktree(tmp_path: Path, name: str = "wt") -> Worktree:
    """A minimal Worktree whose path is a real git checkout (for exclude appends)."""
    wt_path = tmp_path / name
    wt_path.mkdir()
    _git_init(wt_path)
    return Worktree(task_id=name, path=wt_path, branch=f"marshal/{name}")


# --- ordinary file still accepted (anti-blanket-refusal) ---------------------------------------


def test_ordinary_regular_file_is_still_provisioned(tmp_path: Path) -> None:
    """Hardening must not become a blanket refusal of normal single-link files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    src = repo / "notes.md"
    src.write_text("hello from notes", encoding="utf-8")
    wt = _worktree(tmp_path, "ordinary-wt")

    _provision_read_paths(wt, repo, [str(src)], allow_external=True)

    dest = wt.path / ".marshal-context" / "notes.md"
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == "hello from notes"
    assert dest.lstat().st_nlink == 1


# --- 1. hardlink refusal on read_paths ---------------------------------------------------------


def test_read_paths_refuses_hardlink_to_secret_inode(tmp_path: Path) -> None:
    """An innocent name that is a hardlink to a secret inode must not be copied."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    secret = tmp_path / ".env"
    secret.write_text("SECRET=1", encoding="utf-8")
    innocent = repo / "README.md"
    os.link(secret, innocent)
    assert innocent.stat().st_nlink > 1
    assert innocent.stat().st_ino == secret.stat().st_ino
    wt = _worktree(tmp_path, "hl-wt")

    with pytest.raises(ValueError, match="refuses hardlink"):
        _provision_read_paths(wt, repo, [str(innocent)], allow_external=True)

    assert not (wt.path / ".marshal-context" / "README.md").exists()


def test_validate_read_path_tree_refuses_hardlink(tmp_path: Path) -> None:
    """Early naming pass also refuses hardlinks (same policy as the copy walk)."""
    secret = tmp_path / "real-secret"
    secret.write_text("x", encoding="utf-8")
    alias = tmp_path / "alias.txt"
    os.link(secret, alias)

    with pytest.raises(ValueError, match="refuses hardlink"):
        _validate_read_path_tree(alias, str(alias))


# --- 2. case-insensitive secret denylist -------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [".ENV", ".Env.Local", "ID_RSA", "Id_Ed25519", "CERT.PEM", ".NPMRC"],
)
def test_secret_name_refusal_is_case_insensitive(name: str) -> None:
    assert _is_refused_read_path(Path("/tmp") / name)


@pytest.mark.parametrize(
    "parts",
    [
        (".SSH", "config"),
        (".Aws", "credentials"),
        (".GNUPG", "pubring.kbx"),
        (".CONFIG", "GH", "hosts.yml"),
    ],
)
def test_secret_directory_refusal_is_case_insensitive(parts: tuple[str, ...]) -> None:
    assert _is_refused_read_path(Path("/home/u").joinpath(*parts))


def test_read_paths_refuses_case_variant_secret_file(tmp_path: Path) -> None:
    """End-to-end: ``.ENV`` is refused the same way ``.env`` is."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    secret = repo / ".ENV"
    secret.write_text("SECRET=1", encoding="utf-8")
    wt = _worktree(tmp_path, "case-wt")

    with pytest.raises(ValueError, match="refuses secret-shaped path"):
        _provision_read_paths(wt, repo, [str(secret)], allow_external=True)


# --- 3. harvest_artifacts skips hardlinks ------------------------------------------------------


def test_harvest_artifacts_skips_hardlink_escape(tmp_path: Path) -> None:
    """A hardlink under ``.marshal-artifacts/`` must not be copied into durable storage."""
    wt = _worktree(tmp_path, "art-wt")
    art = wt.path / ARTIFACT_DIR
    art.mkdir()
    outside = tmp_path / "host-secret.txt"
    outside.write_text("host content", encoding="utf-8")
    stolen = art / "stolen.txt"
    os.link(outside, stolen)
    (art / "real.md").write_text("legitimate", encoding="utf-8")
    dest = tmp_path / "harvested"
    dest.mkdir()

    stored = harvest_artifacts(wt, dest)

    assert stored == ["real.md"]
    assert not (dest / "stolen.txt").exists()
    assert (dest / "real.md").read_text(encoding="utf-8") == "legitimate"


# --- 4. destination intermediate TOCTOU --------------------------------------------------------


def test_dest_root_symlink_swap_after_ensure_refuses_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapping ``.marshal-context`` to a symlink after the plain-dir check must not escape.

    Path-based ``mkdir``/``os.open`` follow intermediate symlinks; the pinned dest fd +
    ``O_NOFOLLOW`` open closes that window. If the fix is reverted to path-only writes, content
    lands outside the worktree.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    src = repo / "doc.md"
    src.write_text("payload", encoding="utf-8")
    wt = _worktree(tmp_path, "toctou-wt")
    outside = tmp_path / "outside-store"
    outside.mkdir()

    real_contained = provisioning_mod._ensure_contained

    def _swap_after_contained(dest_root: Path, worktree: Worktree, *, what: str) -> Path:
        resolved = real_contained(dest_root, worktree, what=what)
        ctx = worktree.path / ".marshal-context"
        ctx.rmdir()
        ctx.symlink_to(outside, target_is_directory=True)
        return resolved

    monkeypatch.setattr(provisioning_mod, "_ensure_contained", _swap_after_contained)

    with pytest.raises(ValueError, match=r"destination directory open|not a plain directory"):
        _provision_read_paths(wt, repo, [str(src)], allow_external=True)

    assert list(outside.iterdir()) == [], "provisioning wrote through the swapped symlink"


# --- 5. docstring: 0o555 is not owner-proof ----------------------------------------------------


def test_make_readonly_docstring_does_not_claim_owner_cannot_mutate() -> None:
    """0o555 does not stop an owning agent from chmod'ing back; the docstring must not claim it."""
    doc = _make_readonly.__doc__ or ""
    assert "soft guard" in doc or "not a hard" in doc or "chmod" in doc
    assert "stops the agent (as owner) from unlinking" not in doc
