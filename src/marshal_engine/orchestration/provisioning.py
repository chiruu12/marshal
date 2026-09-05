"""Provision a worktree's read-only context: declared `context_files` and `read_paths`.

Copying caller-named paths into an agent's worktree is the one place Marshal reads arbitrary
filesystem locations, so it is written to fail closed: symlinks, special files, hardlinks, and
paths escaping their root are refused, and every copy re-checks identity at open time rather
than trusting an earlier `stat` (the TOCTOU races in #105).
"""

from __future__ import annotations

import contextvars
import fnmatch
import os
import shutil
import stat
from pathlib import Path

from ..core.ids import validate_run_id
from ..runtime.git_exclude import GitExcludeError, append_git_exclude
from ..runtime.worktree import Worktree


def _require_context_files(wt: Worktree, context_files: list[str]) -> None:
    """Fail the run if a declared context file is not actually in the worktree.

    A worktree contains TRACKED files. A path that is gitignored - `tmp/`, a build dir, a scratch
    report - exists in the driver's checkout and simply is not there, so the agent was told to read
    a file it cannot open. Observed in the field: the agent reported the file "was not present",
    worked from the surrounding prose instead, and produced something that happened to be adequate.
    Neither side could tell it had solved a different problem than the one posed.

    Failing is deliberate, over silently copying the file in. Copying would put untracked content
    into a worktree whose whole purpose is to mirror the repo - and `.env` is gitignored too, so
    "copy whatever the caller named" is a way to hand secrets to an agent. Fail-closed matches
    task_id validation, worktree containment, and read-only reviewer routing.

    Containment is checked BEFORE existence, and is the more important half. ``Path("/wt") /
    "/etc/passwd"`` is ``/etc/passwd`` - an absolute path silently discards the base - and ``../``
    walks out the same way. Both exist, so an existence-only check would pass them and then inject
    the path into the agent's prompt, pointing it at host files the worktree boundary is there to
    keep out. A check that accepts those is worse than no check: it makes the path look validated.
    """
    outside: list[str] = []
    missing: list[str] = []
    base = wt.path.resolve()
    for f in context_files:
        candidate = (base / f).resolve()
        if candidate != base and base not in candidate.parents:
            outside.append(f)
        elif not candidate.exists():
            missing.append(f)
    if outside:
        raise ValueError(
            f"context_files outside the worktree: {', '.join(sorted(outside))}. "
            f"Paths must be relative to the repo root and stay inside it - the worktree is the "
            f"isolation boundary, so absolute paths and '..' are refused."
        )
    if missing:
        raise ValueError(
            f"context_files not present in the worktree: {', '.join(sorted(missing))}. "
            f"A worktree holds tracked files only, so gitignored or untracked paths are absent - "
            f"commit them, or put the content in the goal text instead of pointing at a path."
        )


# Destination directory inside each worktree for declared read_paths copies. Appended to
# `.git/info/exclude` so the copies never appear in the run's diff or changed_files.
_READ_CONTEXT_DIR = ".marshal-context"

# Fail-closed secret shapes. Same hazard as silently copying for context_files: gitignored secrets
# (`.env`, keys under `.ssh`) exist on the driver's machine and must not be handed to an agent.
_READ_PATH_SECRET_NAME_GLOBS = (
    ".env*", "*.pem", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    # Credential files that are not key-shaped and were not covered by the original list. Every one
    # of these is a plain file in a predictable place holding a live token: copying it into a
    # worktree hands it to the agent, and therefore to a model provider.
    ".netrc", "_netrc", "credentials", "*.kdbx", "*.p12", "*.pfx", "*.key",
    ".npmrc", ".pypirc", ".dockercfg", "hosts.yml", "hosts.json", "gh_token*",
)

#: Directory names whose contents are credentials regardless of the file names inside them.
_READ_PATH_SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config/gh"})

# Casefolded forms for the denylist. ``fnmatch`` is case-sensitive on POSIX, and set membership
# on path parts is exact — without folding, ``.ENV`` / ``ID_RSA`` / ``.SSH/`` would slip through.
_READ_PATH_SECRET_NAME_GLOBS_CF = tuple(p.casefold() for p in _READ_PATH_SECRET_NAME_GLOBS)
_READ_PATH_SECRET_DIRS_CF = frozenset(d.casefold() for d in _READ_PATH_SECRET_DIRS)

# Directory inside each worktree where an agent writes output meant to OUTLIVE the worktree.
# Excluded from git like `.marshal-context/`, so writing here never pollutes the run's diff -
# an artifact is a report about the work, not part of it.
ARTIFACT_DIR = ".marshal-artifacts"

# Where a prior run's harvested artifacts are mounted for a later one, under `.marshal-context/`.
# Nested there so one exclude entry still covers every injected read, and so the agent sees all
# its read-only inputs in one place.
ARTIFACT_MOUNT = "artifacts"


def _is_refused_read_path(path: Path) -> bool:
    """True when ``path`` is credential-shaped by name, or sits in a credential directory.

    A denylist cannot be complete, which is why it is no longer the only thing standing between an
    agent and the host's secrets - `read_paths` is scoped to the repo unless the operator opts out
    (see `_ensure_read_path_in_scope`). This list is the second layer, for the case where a secret
    lives inside the repo itself.

    Matching is case-insensitive: credential files are often created with variant casing
    (``.ENV``, ``Id_Rsa``), and a case-sensitive filter would miss them on case-sensitive volumes.
    """
    parts = path.parts
    if any(part.casefold() in _READ_PATH_SECRET_DIRS_CF for part in parts):
        return True
    # Two-segment directory names (".config/gh") need a pairwise check.
    if any(
        f"{a}/{b}".casefold() in _READ_PATH_SECRET_DIRS_CF
        for a, b in zip(parts, parts[1:], strict=False)
    ):
        return True
    name_cf = path.name.casefold()
    return any(fnmatch.fnmatch(name_cf, pat) for pat in _READ_PATH_SECRET_NAME_GLOBS_CF)


def _refuse_hardlink(path: Path, st: os.stat_result) -> None:
    """Refuse a regular file with ``st_nlink > 1`` (shared inode under another name).

    An innocent basename that is a hardlink to a secret inode would otherwise be copied by the
    regular-file path — the module promises hardlink refusal; this is that check. Directories
    naturally have ``st_nlink >= 2`` (``.`` / ``..``); only regular files are gated.
    """
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        raise ValueError(
            f"read_paths refuses hardlink: {path}. "
            f"A hardlink shares an inode with another name, so copying it would duplicate "
            f"content that may be a secret under a different path."
        )


def _ensure_read_path_in_scope(src: Path, raw: str, repo_root: Path, allow_external: bool) -> None:
    """Refuse a `read_path` outside the workspace's own repo unless the operator opted in.

    The denylist above is a guess about which names hold secrets, and a guess cannot cover a host's
    whole filesystem. Scope can: a path inside the repo is content the operator already trusts this
    workspace with, and everything else needs a deliberate `allow_external_read_paths: true`.

    This is also what closes the cross-workspace read channel. `read_paths` pointing at another
    workspace's `.marshal/runs` would copy that workspace's ledger into this one's worktree, which
    contradicts the tenancy claim that each workspace keeps its own state - and no name-based rule
    would have caught it, because there is nothing secret-shaped about the name `runs`.

    Note who the caller is: `read_paths` comes from the driver agent, so "the operator asked for
    it" holds only as far as the driver is trustworthy. Scoping keeps a prompt-injected driver from
    turning a context-provisioning feature into an arbitrary host-file reader.
    """
    if allow_external:
        return
    try:
        inside = src.is_relative_to(repo_root.resolve())
    except (OSError, ValueError):
        inside = False
    if not inside:
        raise ValueError(
            f"read_paths refuses a path outside this workspace's repo: {src} (declared as {raw!r}). "
            f"Set `allow_external_read_paths: true` in fleet.config.yaml to permit it, or copy what "
            f"the agent needs into the repo. Scoping is what keeps this from being a way to read "
            f"any file on the host - including another workspace's ledger."
        )


def _resolve_read_path(raw: str, repo_root: Path) -> Path:
    """Resolve a declared read_path: absolute as-is, else relative to the driver's repo root."""
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    return (repo_root / p).resolve()


def _iter_read_path_copy_targets(src: Path) -> list[Path]:
    """Return ``src`` and every descendant that a guarded tree copy would touch.

    Walks with ``iterdir`` and does not follow directory symlinks, so a link is listed as itself
    (for refusal) rather than expanding into a foreign tree. Listing only (no open) so a FIFO
    descendant is discoverable without hanging.
    """
    targets = [src]
    if not src.is_dir() or src.is_symlink():
        return targets
    stack = [src]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            targets.append(entry)
            if entry.is_dir() and not entry.is_symlink():
                stack.append(entry)
    return targets


def _validate_read_path_tree(src: Path, raw: str) -> None:
    """Up-front pass: refuse secret-shaped / symlink / special entries, naming the offender early.

    This is **not** the security boundary. A TOCTOU swap after this pass can still replace the
    tree; ``_copy_read_path_tree`` re-applies the same policy at the point of use (fd-relative
    walk) and pins file/directory identity by ``(st_dev, st_ino)``. Keep both: this pass for a
    clear error before any worktree work; the copy walk to enforce.

    Applied to every path that would be copied, not just the declared root: a directory named
    innocently can still contain ``.env`` / ``.ssh`` / a FIFO / a symlink. Raise loudly naming
    the offender rather than silently skipping - a quietly incomplete tree is the same class of
    failure ``_require_context_files`` exists to prevent.

    Symlinks among descendants are refused outright: a link's meaning depends on where it is
    resolved, so a copied-in link either smuggles host content (dereferenced) or dangles/escapes
    (preserved) — neither belongs in a read-only reference copy. The declared root may itself be
    a driver-typed symlink (e.g. a linked docs dir); callers resolve it first, then this check
    applies to descendants of the real path (so a root link into a secret still fails by
    name/location).
    """
    for path in _iter_read_path_copy_targets(src):
        if path != src and path.is_symlink():
            try:
                link_target = os.readlink(path)
            except OSError:
                link_target = "?"
            raise ValueError(
                f"read_paths refuses symlink: {path} -> {link_target}. "
                f"Symlinks inside a declared tree are never copied (declared as {raw!r})."
            )
        if _is_refused_read_path(path):
            raise ValueError(
                f"read_paths refuses secret-shaped path: {path}. "
                f"Credential-shaped names (dotenv files, private keys, .netrc, npm/pypi/docker "
                f"and gh credential files) and anything inside a credential directory (.ssh, .aws, "
                f".gnupg, .kube, .docker) are never copied into a worktree - including descendants "
                f"of {raw!r}."
            )
        if not path.is_dir() and not path.is_file():
            raise ValueError(
                f"read_paths refuses special file: {path}. "
                f"Only regular files and directories are accepted "
                f"(FIFOs, sockets, and devices block provisioning before any run timeout; "
                f"declared as {raw!r})."
            )
        if path.is_file() and not path.is_symlink():
            _refuse_hardlink(path, path.lstat())


def _guarded_copy_file(
    src: Path,
    dest: Path,
    *,
    dir_fd: int | None = None,
    expected_id: tuple[int, int],
    dest_dir_fd: int | None = None,
    dest_name: str | None = None,
) -> None:
    """Copy one file through a fail-closed open: no follow, non-blocking, regular-file only.

    ``O_NOFOLLOW`` refuses a symlink swapped in after validation; ``O_NONBLOCK`` means opening a
    FIFO returns instead of blocking; ``fstat`` confirms a regular file before any read, and its
    ``(st_dev, st_ino)`` must match ``expected_id`` from the classifying ``lstat``, which refuses
    a swap to a DIFFERENT regular file. Identity is a secondary check, not the boundary: a
    delete-then-recreate at the same path can be handed back the same inode (Linux reuses freed
    inodes readily). Hardlinks (``st_nlink > 1``) are refused so an innocent name cannot smuggle
    a secret inode. Destination create uses ``O_CREAT|O_EXCL`` (and ``dir_fd`` when set) so an
    existing symlink at the final component is never followed and intermediate destination
    components are not re-resolved through a swapped symlink. When ``dir_fd`` is set, opens
    ``src.name`` relative to that descriptor (no absolute-path reopen). When ``dest_dir_fd`` is
    set, creates the destination relative to that descriptor via ``dest_name``.
    """
    open_name: str | Path = src.name if dir_fd is not None else src
    try:
        fd = (
            os.open(open_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
            if dir_fd is not None
            else os.open(open_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        )
    except OSError as exc:
        raise ValueError(
            f"read_paths refused to open {src}: {exc}. "
            f"Only regular files are copied (symlinks and special files are refused)."
        ) from exc
    out_fd: int | None = None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(
                f"read_paths refuses special file: {src}. "
                f"Only regular files and directories are accepted "
                f"(FIFOs, sockets, and devices block provisioning before any run timeout)."
            )
        _refuse_hardlink(src, opened)
        if (opened.st_dev, opened.st_ino) != expected_id:
            raise ValueError(
                f"read_paths refuses swapped file: {src}. "
                f"File was replaced between classification and open."
            )
        try:
            # O_EXCL: never open/follow an existing destination (including a symlink).
            # Prefer dest_dir_fd so a swapped intermediate directory cannot redirect the write.
            if dest_dir_fd is not None:
                out_fd = os.open(
                    dest_name if dest_name is not None else dest.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                    dir_fd=dest_dir_fd,
                )
            else:
                out_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except OSError as exc:
            raise ValueError(
                f"read_paths refuses destination that already exists: {dest}. "
                f"Copies never follow or replace an existing path at the destination "
                f"(including a symlink)."
            ) from exc
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            os.write(out_fd, chunk)
    finally:
        os.close(fd)
        if out_fd is not None:
            os.close(out_fd)


def _lstat_at(src: Path, *, dir_fd: int | None) -> os.stat_result:
    """``lstat`` ``src``, or ``src.name`` relative to ``dir_fd`` when set."""
    if dir_fd is None:
        return src.lstat()
    return os.lstat(src.name, dir_fd=dir_fd)


def _readlink_at(src: Path, *, dir_fd: int | None) -> str:
    """``readlink`` ``src``, or ``src.name`` relative to ``dir_fd`` when set."""
    try:
        if dir_fd is None:
            return os.readlink(src)
        return os.readlink(src.name, dir_fd=dir_fd)
    except OSError:
        return "?"


def _open_dir_nofollow(src: Path, *, dir_fd: int | None) -> int:
    """Open a directory with ``O_RDONLY|O_NOFOLLOW|O_DIRECTORY`` (path or relative to ``dir_fd``).

    ``O_NOFOLLOW`` refuses a directory swapped to a symlink; ``O_DIRECTORY`` refuses a
    non-directory. On failure, raise the same refusal messages as the lstat classification path.
    """
    open_name: str | Path = src.name if dir_fd is not None else src
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    try:
        if dir_fd is not None:
            return os.open(open_name, flags, dir_fd=dir_fd)
        return os.open(open_name, flags)
    except OSError as exc:
        try:
            st_now = _lstat_at(src, dir_fd=dir_fd)
        except OSError:
            raise ValueError(
                f"read_paths refused to open {src}: {exc}. "
                f"Only regular files are copied (symlinks and special files are refused)."
            ) from exc
        if stat.S_ISLNK(st_now.st_mode):
            raise ValueError(
                f"read_paths refuses symlink: {src} -> {_readlink_at(src, dir_fd=dir_fd)}. "
                f"Symlinks inside a declared tree are never copied."
            ) from exc
        if not stat.S_ISDIR(st_now.st_mode) and not stat.S_ISREG(st_now.st_mode):
            raise ValueError(
                f"read_paths refuses special file: {src}. "
                f"Only regular files and directories are accepted "
                f"(FIFOs, sockets, and devices block provisioning before any run timeout)."
            ) from exc
        raise ValueError(
            f"read_paths refused to open {src}: {exc}. "
            f"Only regular files are copied (symlinks and special files are refused)."
        ) from exc


def _refuse_existing_dest_symlink(
    dest: Path,
    *,
    dest_dir_fd: int | None = None,
    dest_name: str | None = None,
) -> None:
    """Refuse a destination path that already exists as a symlink (never follow or replace it)."""
    try:
        if dest_dir_fd is not None:
            st = os.lstat(dest_name if dest_name is not None else dest.name, dir_fd=dest_dir_fd)
        else:
            st = dest.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(
            f"read_paths refuses destination symlink: {dest}. "
            f"Copies never follow a symlink on the destination side."
        )


def _open_plain_dest_dir(path: Path, *, what: str) -> int:
    """Open ``path`` as a plain directory with ``O_NOFOLLOW|O_DIRECTORY``; pin identity.

    Closes the destination-side TOCTOU where a path-based ``mkdir`` / ``os.open`` after a one-shot
    plain-directory check would follow a swapped intermediate symlink. Callers create children
    relative to the returned fd.
    """
    try:
        st = path.lstat()
    except OSError as exc:
        raise ValueError(
            f"{what} destination disappeared before open: {path}: {exc}"
        ) from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ValueError(
            f"{what} refuses a worktree `{path.name}` that is not a plain directory: {path}. "
            f"The worktree already contains a `{path.name}` that is not a plain directory; "
            f"refuse rather than follow or replace it."
        )
    expected_id = (st.st_dev, st.st_ino)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError as exc:
        raise ValueError(
            f"{what} refuses destination directory open: {path}: {exc}. "
            f"Copies never follow a symlink on the destination side."
        ) from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != expected_id or not stat.S_ISDIR(opened.st_mode):
            raise ValueError(
                f"{what} refuses swapped destination directory: {path}. "
                f"Directory was replaced between classification and open."
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


#: What the caller is doing, for the refusal message. `_copy_read_path_tree` is shared by
#: `read_paths` and `artifacts_from`, and naming the wrong one sent a driver to fix a setting it
#: had not used.
_COPY_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "marshal_copy_context", default="read_paths"
)


def _copy_read_path_tree(
    src: Path,
    dest: Path,
    *,
    dir_fd: int | None = None,
    logical: Path | None = None,
    dest_dir_fd: int | None = None,
    dest_name: str | None = None,
) -> None:
    """Copy a resolved read_path into ``dest``; enforce read_paths policy at the point of use.

    This walk is the security boundary (``_validate_read_path_tree`` is only an early-naming
    pass). Every entry discovered via ``os.scandir(dir_fd)`` is checked before copy or descent:
    secret-shaped name / ``.ssh`` component (via the walk's logical path), symlink refusal,
    hardlink refusal, and regular-file-or-directory-only. Directory descent is fd-relative
    (``O_RDONLY|O_NOFOLLOW|O_DIRECTORY`` + scandir on the fd); after open, ``fstat`` must match
    the classifying ``lstat``'s ``(st_dev, st_ino)``, which refuses a swap to a DIFFERENT
    directory. File opens do the same identity pin (refuse a swap to a DIFFERENT regular file).
    Identity is a secondary check, not the boundary: a delete-then-recreate at the same path can
    be handed back the same inode (Linux reuses freed inodes readily), so the per-entry policy
    checks above are what actually contain a swapped tree. Per-file open stays fail-closed
    (``O_RDONLY|O_NOFOLLOW|O_NONBLOCK`` + ``fstat`` + identity). Destination creates are
    fd-relative when ``dest_dir_fd`` is set (``mkdirat`` / ``openat`` with ``O_EXCL``) so a
    swapped intermediate directory cannot redirect writes; existing dest symlinks refused.
    Does not follow or preserve source symlinks.
    """
    logical_path = logical if logical is not None else src
    child_dest_name = dest_name if dest_name is not None else dest.name
    if _is_refused_read_path(logical_path):
        # The pattern list is read from the constants rather than restated: the hardcoded prose
        # had drifted to a subset of what actually refuses, so a driver could not tell why its
        # file was rejected or which of its files to rename.
        globs = ", ".join(_READ_PATH_SECRET_NAME_GLOBS)
        dirs = ", ".join(sorted(_READ_PATH_SECRET_DIRS))
        raise ValueError(
            f"{_COPY_CONTEXT.get()} refuses secret-shaped path: {logical_path}. "
            f"Names matching {globs} - or anything inside {dirs} - are never copied into a "
            "worktree. Rename or remove that file, or leave it out of the run."
        )
    try:
        st = _lstat_at(src, dir_fd=dir_fd)
    except OSError as exc:
        raise ValueError(f"read_paths disappeared before copy: {src}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(
            f"read_paths refuses symlink: {src} -> {_readlink_at(src, dir_fd=dir_fd)}. "
            f"Symlinks inside a declared tree are never copied."
        )
    if stat.S_ISDIR(st.st_mode):
        expected_id = (st.st_dev, st.st_ino)
        _refuse_existing_dest_symlink(
            dest, dest_dir_fd=dest_dir_fd, dest_name=child_dest_name
        )
        try:
            if dest_dir_fd is not None:
                os.mkdir(child_dest_name, dir_fd=dest_dir_fd)
            else:
                dest.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise ValueError(
                f"read_paths refuses destination that already exists: {dest}. "
                f"Copies never follow or replace an existing path at the destination."
            ) from exc
        this_fd = _open_dir_nofollow(src, dir_fd=dir_fd)
        new_dest_fd: int | None = None
        try:
            opened = os.fstat(this_fd)
            if (opened.st_dev, opened.st_ino) != expected_id:
                raise ValueError(
                    f"read_paths refuses swapped directory: {src}. "
                    f"Directory was replaced between classification and open."
                )
            try:
                if dest_dir_fd is not None:
                    new_dest_fd = os.open(
                        child_dest_name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                        dir_fd=dest_dir_fd,
                    )
                else:
                    new_dest_fd = os.open(
                        dest, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
                    )
            except OSError as exc:
                raise ValueError(
                    f"read_paths refuses destination directory open: {dest}: {exc}. "
                    f"Copies never follow a symlink on the destination side."
                ) from exc
            with os.scandir(this_fd) as entries:
                children = sorted(entries, key=lambda e: e.name)
            for entry in children:
                # Policy at point of use — do not trust the up-front validate pass.
                _copy_read_path_tree(
                    src / entry.name,
                    dest / entry.name,
                    dir_fd=this_fd,
                    logical=logical_path / entry.name,
                    dest_dir_fd=new_dest_fd,
                    dest_name=entry.name,
                )
        finally:
            os.close(this_fd)
            if new_dest_fd is not None:
                os.close(new_dest_fd)
        return
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(
            f"read_paths refuses special file: {src}. "
            f"Only regular files and directories are accepted "
            f"(FIFOs, sockets, and devices block provisioning before any run timeout)."
        )
    _refuse_hardlink(src, st)
    _refuse_existing_dest_symlink(dest, dest_dir_fd=dest_dir_fd, dest_name=child_dest_name)
    _guarded_copy_file(
        src,
        dest,
        dir_fd=dir_fd,
        expected_id=(st.st_dev, st.st_ino),
        dest_dir_fd=dest_dir_fd,
        dest_name=child_dest_name,
    )


def _make_readonly(path: Path) -> None:
    """chmod copied content read-only for the agent: files 0o444, directories 0o555.

    Clearing write bits is a soft guard against accidental agent edits of provisioned
    reference material under the git-excluded ``.marshal-context/``. It is not a hard
    immutability boundary: the agent owns these paths and can ``chmod`` them back to
    writable, then unlink or replace enclosed files. Teardown restores owner-write on
    directories in ``WorktreeManager.remove`` / ``discard`` before ``git worktree remove`` /
    ``rmtree``, so a tree without write bits cannot strand a worktree.
    """
    if path.is_dir():
        # 0o555 still allows traversal, so chmod during the walk is safe.
        os.chmod(path, 0o555)
        for child in path.rglob("*"):
            os.chmod(child, 0o555 if child.is_dir() else 0o444)
    else:
        os.chmod(path, 0o444)


def _append_exclude(wt: Worktree, entry: str) -> None:
    """Append ``entry`` to the worktree's ``info/exclude`` if not already present.

    Strict here on purpose: a read_paths copy that is NOT excluded would show up in the run's diff
    and in `changed_files`, so the caller would report provisioned context as the agent's work.
    """
    try:
        append_git_exclude(wt.path, entry)
    except GitExcludeError as exc:
        raise ValueError(str(exc)) from exc


def _ensure_plain_dir(path: Path, *, what: str) -> Path:
    """Return ``path`` as a plain directory; refuse a pre-existing symlink or non-dir.

    Do **not** ``resolve()`` through ``path`` before this check: a tracked symlink would be
    followed and provisioning would copy into (and chmod) the link target. Create with ``mkdir``
    only when absent; never silently replace or follow.

    Every component a caller creates needs this, not just the first. ``mkdir(parents=True,
    exist_ok=True)`` is not a substitute: its ``exist_ok`` branch tests ``is_dir()``, which
    follows symlinks, so it accepts a link pointing anywhere and writes land at the target.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=False)
    else:
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ValueError(
                f"{what} refuses a worktree `{path.name}` that is not a plain directory: {path}. "
                f"The worktree already contains a `{path.name}` that is not a plain directory; "
                f"refuse rather than follow or replace it."
            )
    return path


def _ensure_plain_marshal_context_dir(dest_root: Path) -> Path:
    """``_ensure_plain_dir`` for the ``.marshal-context`` root every read mount hangs off."""
    return _ensure_plain_dir(dest_root, what="read_paths")


def _ensure_contained(dest_root: Path, wt: Worktree, *, what: str) -> Path:
    """Resolve ``dest_root`` and prove it is still inside ``wt``. Returns the resolved path.

    Safe to resolve at this point only because every component was already refused as a symlink
    by ``_ensure_plain_dir`` - this is the belt to that braces, and catches a component swapped
    between the check and the copy.
    """
    resolved = dest_root.resolve()
    base = wt.path.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"{what} destination escaped the worktree: {resolved}")
    return resolved


def _provision_read_paths(
    wt: Worktree, repo_root: Path, read_paths: list[str], *, allow_external: bool = False
) -> None:
    """Copy declared outside-worktree paths into ``.marshal-context/`` as read-only.

    Unlike ``context_files`` (which must already be *in* the worktree), ``read_paths`` are
    deliberately outside - absolute, or relative to the driver's repo root. They are copied in so
    the agent can read them without breaking the worktree isolation boundary for writes.

    Secret-shaped paths are refused on the declared root **and every descendant** that would be
    copied (a directory named innocently must not smuggle ``.env`` / keys under ``.ssh``). Symlinks
    and hardlinks inside a declared tree are refused (a link either smuggles host content when
    dereferenced or shares a secret inode; the declared root may be a symlink and is resolved
    first). Only regular files (``st_nlink == 1``) and directories are accepted - FIFOs, sockets,
    and devices are refused so provisioning cannot block forever before a run record (and its
    timeout) exists. Policy is enforced during the fd-relative copy walk (validation at point of
    use): every scandir entry is re-checked, and each file/directory's ``(st_dev, st_ino)`` from
    the classifying ``lstat`` must match ``fstat`` of the opened fd so a same-type swap cannot
    smuggle unvalidated content (identity is secondary — delete-then-recreate can reuse an
    inode). The up-front ``_validate_read_path_tree`` pass only names offenders early — it is
    not the security boundary. Destination ``.marshal-context`` must be absent or a plain
    directory (a tracked symlink or non-dir is refused; never ``resolve()`` through it). Per-entry
    destinations are created relative to a pinned destination directory fd so a swapped
    intermediate cannot redirect writes. Fail-closed matches task_id validation, worktree
    containment, and read-only reviewer routing.

    A missing path fails before any copy. Copied files are 0o444 and directories 0o555; teardown
    restores directory write bits so ``git worktree remove`` still works. Content is excluded
    from git via ``.git/info/exclude`` so it never pollutes the run's diff.
    """
    if not read_paths:
        return

    resolved: list[tuple[str, Path]] = []
    for raw in read_paths:
        src = _resolve_read_path(raw, repo_root)
        _ensure_read_path_in_scope(src, raw, repo_root, allow_external)
        if not src.exists():
            raise ValueError(
                f"read_paths not found: {raw!r}. "
                f"Paths must exist (absolute, or relative to the driver's repo root) before the "
                f"run is provisioned."
            )
        name = src.name
        if name in ("", ".", ".."):
            raise ValueError(
                f"read_paths has an unusable basename: {raw!r}. "
                f"Each path must resolve to a named file or directory."
            )
        # Early naming only — not the security boundary. Policy is enforced in
        # _copy_read_path_tree (at point of use) with (dev, ino) pinning.
        _validate_read_path_tree(src, raw)
        resolved.append((raw, src))

    dest_root = _ensure_plain_marshal_context_dir(wt.path / _READ_CONTEXT_DIR)
    # Containment: every copy lands under `.marshal-context/` inside this worktree.
    dest_root = _ensure_contained(dest_root, wt, what="read_paths")
    # Pin the destination directory fd so path-based writes cannot follow a swapped
    # intermediate symlink between the plain-dir check and each mkdir/open.
    dest_root_fd = _open_plain_dest_dir(dest_root, what="read_paths")

    seen_names: dict[str, str] = {}
    try:
        for raw, src in resolved:
            name = src.name
            if name in seen_names:
                raise ValueError(
                    f"read_paths basename collision: {raw!r} and {seen_names[name]!r} both map to "
                    f".marshal-context/{name}."
                )
            seen_names[name] = raw
            # Single basename under dest_root — create relative to the pinned fd (never
            # re-resolve through a pre-existing dest symlink into tracked content).
            dest = dest_root / name
            try:
                dest.relative_to(dest_root)
            except ValueError as exc:
                raise ValueError(
                    f"read_paths destination escaped .marshal-context/: {raw!r} -> {dest}"
                ) from exc
            try:
                existing = os.lstat(name, dir_fd=dest_root_fd)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if stat.S_ISLNK(existing.st_mode):
                    raise ValueError(
                        f"read_paths refuses destination symlink: {dest}. "
                        f"Copies never follow a symlink on the destination side."
                    )
                raise ValueError(
                    f"read_paths refuses destination that already exists: {dest}. "
                    f"Copies never follow or replace an existing path at the destination."
                )
            _copy_read_path_tree(src, dest, dest_dir_fd=dest_root_fd, dest_name=name)
            _make_readonly(dest)
    finally:
        os.close(dest_root_fd)

    _append_exclude(wt, f"{_READ_CONTEXT_DIR}/")


def prepare_artifact_dir(wt: Worktree) -> None:
    """Create the worktree's ``.marshal-artifacts/`` and keep it out of git.

    Created up front rather than left to the agent so the directory is always there to write into,
    and so the exclude entry exists before anything lands in it - an artifact appearing in the
    run's own diff would make a report about the work look like part of the work.
    """
    # Same refusal as the read mounts: a `.marshal-artifacts` symlink committed in the repo would
    # be followed here, and `harvest_artifacts` would then copy out of the link target.
    _ensure_contained(
        _ensure_plain_dir(wt.path / ARTIFACT_DIR, what="artifacts"), wt, what="artifacts"
    )
    _append_exclude(wt, f"{ARTIFACT_DIR}/")


def harvest_artifacts(wt: Worktree, dest_root: Path) -> list[str]:
    """Copy the run's ``.marshal-artifacts/`` out to `dest_root`; return the relative names stored.

    Runs at the end of EVERY run, including failed ones: a run that died partway may still have
    written the findings that explain why, and those are exactly what the next round needs.

    Symlinks and hardlinks are skipped, not followed. The agent controls this directory, so a
    link here is a request to copy something the agent chose - possibly outside its worktree -
    into durable storage the driver later mounts into other runs. Dereferencing a symlink or
    copying a hardlinked inode would turn the artifact channel into a worktree-escape, so
    links are dropped and named in the return value's absence.

    Never raises: harvesting happens after the work is done and its record is being stamped, so a
    failure here must not turn a finished run into a failed one.
    """
    src_root = wt.path / ARTIFACT_DIR
    if not src_root.is_dir() or src_root.is_symlink():
        return []
    stored: list[str] = []
    for src in sorted(src_root.rglob("*")):
        try:
            st = src.lstat()
        except OSError:
            continue
        # Skip symlinks, non-regular files, and hardlinks (shared inode → possible escape).
        if (
            stat.S_ISLNK(st.st_mode)
            or not stat.S_ISREG(st.st_mode)
            or st.st_nlink > 1
        ):
            continue
        rel = src.relative_to(src_root)
        dest = dest_root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        except OSError:
            continue
        stored.append(rel.as_posix())
    return stored


def provision_run_artifacts(wt: Worktree, artifacts_root: Path, run_ids: list[str]) -> None:
    """Mount earlier runs' harvested artifacts read-only under ``.marshal-context/artifacts/``.

    Fails loudly when a named run has no artifacts. A driver naming a run it wants the next round
    to build on has stated a dependency; silently handing the agent nothing would let it solve the
    task from the prompt alone and look like it worked - the same silent-missing-input failure
    `context_files` was hardened against.
    """
    if not run_ids:
        return
    # Both components are refused as symlinks, then the result is proved to still be inside the
    # worktree. `artifacts` needs the check as much as `.marshal-context` does: the agent of an
    # earlier run controls its own worktree, and a link committed there rides into this one.
    context_root = _ensure_plain_marshal_context_dir(wt.path / _READ_CONTEXT_DIR)
    dest_root = _ensure_plain_dir(context_root / ARTIFACT_MOUNT, what="artifacts_from")
    dest_root = _ensure_contained(dest_root, wt, what="artifacts_from")
    # Without this the mount is untracked content in the worktree: it lands in the run's own diff
    # and `changed_files`, so provisioned INPUT gets reported as the agent's work and integrate
    # merges it onto the driver's branch. `_provision_read_paths` excludes the same directory, but
    # it returns early when `read_paths` is empty - so `artifacts_from` alone reached here bare.
    _append_exclude(wt, f"{_READ_CONTEXT_DIR}/")
    dest_root_fd = _open_plain_dest_dir(dest_root, what="artifacts_from")
    try:
        for run_id in run_ids:
            # Validated as a path segment: a run id reaching the filesystem must not be able to
            # address a sibling directory (`../`) or escape the artifacts root.
            validate_run_id(run_id)
            src = artifacts_root / run_id
            if not src.is_dir() or not any(src.rglob("*")):
                raise ValueError(
                    f"artifacts_from names a run with no stored artifacts: {run_id!r}. "
                    f"A run only has artifacts if it wrote them to `{ARTIFACT_DIR}/` in its worktree."
                )
            dest = dest_root / run_id
            try:
                existing = os.lstat(run_id, dir_fd=dest_root_fd)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                raise ValueError(f"artifacts_from lists {run_id!r} more than once.")
            token = _COPY_CONTEXT.set("artifacts_from")
            try:
                _copy_read_path_tree(src, dest, dest_dir_fd=dest_root_fd, dest_name=run_id)
            finally:
                _COPY_CONTEXT.reset(token)
            _make_readonly(dest)
    finally:
        os.close(dest_root_fd)


