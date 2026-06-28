"""Workspace registry - let ONE MCP server target several repos, selected per call.

This is the MCP server's *tenancy* layer, kept deliberately OUT of the engine: ``MarshalService``
and ``Fleet`` stay single-repo and know nothing about workspaces. The registry resolves a set of
named repos, lazily builds + caches one (single-repo) ``MarshalService`` per workspace, and resolves
a ``run_id`` back to its owning workspace with a cheap, service-free scan of each repo's run ledger
(a path stat - it never builds a service just to look).

Workspaces are declared in three layers (merged; first declaration of a name/path wins):
  1. the DEFAULT workspace - ``MARSHAL_REPO`` (or cwd), always named "default";
  2. the central registry file - ``~/.marshal/workspaces.yaml`` (override path with
     ``MARSHAL_WORKSPACES_FILE``), the canonical "all config" home: a ``workspaces:`` map of
     ``name: /abs/path`` plus an optional ``max_concurrent:``;
  3. the ``MARSHAL_WORKSPACES`` env var - comma/newline ``name=/abs/path`` entries (back-compat).

Each workspace loads its OWN ``<repo>/fleet.config.yaml`` (clients travel with the repo) and keeps
its OWN isolated ``.marshal`` worktrees + ledger. ``MARSHAL_CONFIG`` is scoped to "default" only.
A ``from_env`` registry hot-reloads the file: a workspace ADDED to it (by hand, ``marshal workspace
add``, or the ``add_workspace`` MCP tool) shows up without reconnecting the server - existing cached
services are untouched. Changing or removing an existing workspace still needs a reconnect. The
process-wide concurrency cap is fixed at startup. Logging is STDERR-only (stdout is JSON-RPC).
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigError, FleetConfig, load_config, validate
from .service import MarshalService
from .state import FleetState, RunRecord

DEFAULT_WORKSPACE = "default"
# Default ceiling on concurrent agent runs when more than one workspace is in play. Each agent CLI
# is 150-400 MB; this keeps an N-workspace fan-out from OOMing the host. Override via the file's
# ``max_concurrent`` or ``MARSHAL_MAX_CONCURRENT``. A lone default workspace (no registry file) stays
# uncapped - exactly today's single-repo behavior.
DEFAULT_MAX_CONCURRENT = 8

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Serialize the read-modify-write of the registry file so two concurrent registrations (e.g. two
# add_workspace tool calls on the MCP worker-thread pool) can't lose an update or race the temp file.
_FILE_WRITE_LOCK = threading.Lock()


def _warn(msg: str) -> None:
    print(f"[marshal] {msg}", file=sys.stderr)


@dataclass(frozen=True)
class WorkspaceDef:
    """A registered workspace: a name, its resolved repo path, and the config it loads."""

    name: str
    path: Path
    config_path: Path


# --- the central registry file (~/.marshal/workspaces.yaml) -----------------------------------


def workspaces_file_path(environ: Mapping[str, str] | None = None) -> Path:
    """Path to the central registry file (``MARSHAL_WORKSPACES_FILE`` or ``~/.marshal/workspaces.yaml``)."""
    env = os.environ if environ is None else environ
    raw = env.get("MARSHAL_WORKSPACES_FILE")
    return Path(raw).expanduser() if raw else Path.home() / ".marshal" / "workspaces.yaml"


def read_workspaces_file(path: Path | str) -> tuple[dict[str, str], int | None]:
    """Parse the registry file into ``(name -> raw_path, max_concurrent)``. Total + crash-proof.

    A missing file is ``({}, None)``; a malformed file warns to stderr and is treated as empty, so a
    bad file can never crash the server on connect.
    """
    p = Path(path)
    if not p.exists():
        return {}, None
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError, ValueError) as exc:
        # YAMLError (bad syntax), OSError (a directory / unreadable file at the path), ValueError
        # (incl. UnicodeDecodeError on a binary file). A bad registry must never crash on connect.
        _warn(f"ignoring unreadable workspaces file {p}: {exc}")
        return {}, None
    if not isinstance(raw, dict):
        _warn(f"ignoring workspaces file {p}: expected a mapping at the top level")
        return {}, None
    workspaces: dict[str, str] = {}
    ws_raw = raw.get("workspaces")
    if isinstance(ws_raw, dict):
        for name, val in ws_raw.items():
            if val:
                workspaces[str(name)] = str(val)
    elif ws_raw is not None:
        _warn(f"workspaces file {p}: 'workspaces' must be a mapping of name -> path; ignoring it")
    max_concurrent: int | None = None
    mc = raw.get("max_concurrent")
    if mc is not None:
        if isinstance(mc, bool) or not isinstance(mc, int) or mc <= 0:
            _warn(f"workspaces file {p}: max_concurrent must be a positive integer; ignoring")
        else:
            max_concurrent = mc
    return workspaces, max_concurrent


def _write_workspaces_file(path: Path, workspaces: dict[str, str], max_concurrent: int | None) -> None:
    """Atomically write the registry file (UNIQUE temp + replace), preserving ``max_concurrent``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {}
    if max_concurrent is not None:
        doc["max_concurrent"] = max_concurrent
    doc["workspaces"] = dict(sorted(workspaces.items()))
    # A unique temp in the same dir (not a fixed name) so concurrent writers can't clobber each
    # other's temp before the atomic os.replace.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(yaml.safe_dump(doc, sort_keys=False))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def register_workspace(
    name: str,
    path: Path | str,
    *,
    file_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> WorkspaceDef:
    """Add (or update) a workspace in the central registry file. Returns its resolved definition.

    Validates the name (``[A-Za-z0-9._-]``, not "default") and that the path is an existing
    directory. The repo need not have a ``fleet.config.yaml`` yet - it registers with zero clients
    until one is added (use ``scaffold_fleet_config`` to drop a starter in).
    """
    if name == DEFAULT_WORKSPACE:
        raise ValueError("'default' is reserved for the MARSHAL_REPO workspace")
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid workspace name {name!r}; use letters, digits, '.', '_' or '-'")
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"path is not an existing directory: {resolved}")
    fpath = Path(file_path) if file_path else workspaces_file_path(environ)
    with _FILE_WRITE_LOCK:
        workspaces, max_concurrent = read_workspaces_file(fpath)
        workspaces[name] = str(resolved)
        _write_workspaces_file(fpath, workspaces, max_concurrent)
    return WorkspaceDef(name, resolved, resolved / "fleet.config.yaml")


def remove_workspace(
    name: str, *, file_path: Path | str | None = None, environ: Mapping[str, str] | None = None
) -> bool:
    """Remove a workspace from the registry file. Returns False if it wasn't there."""
    fpath = Path(file_path) if file_path else workspaces_file_path(environ)
    with _FILE_WRITE_LOCK:
        workspaces, max_concurrent = read_workspaces_file(fpath)
        if name not in workspaces:
            return False
        del workspaces[name]
        _write_workspaces_file(fpath, workspaces, max_concurrent)
    return True


_FLEET_STUB = """\
# Marshal fleet config: declare the worker clients Marshal can route tasks to. Each client pins a
# backend + model + permission. See fleet.config.example.yaml in the Marshal repo for the full
# reference (opencode-go, EastRouter, usage_api, worktree_setup, context, etc.).
defaults:
  permission: safe-edit
  timeout_s: 900

clients: {}
  # example - uncomment and edit, then run `marshal doctor` to verify the backend is available:
  # claude:
  #   backend: claude-code
  #   model: claude-sonnet-4-6
"""


def scaffold_fleet_config(repo: Path | str) -> bool:
    """Drop a starter ``fleet.config.yaml`` into a repo that has none. Returns False if one exists."""
    cfg = Path(repo) / "fleet.config.yaml"
    if cfg.exists():
        return False
    cfg.write_text(_FLEET_STUB, encoding="utf-8")
    return True


# --- resolution: default + file + env --------------------------------------------------------


def _split_entries(value: str) -> list[str]:
    """Split a MARSHAL_WORKSPACES value on commas and newlines, dropping blanks."""
    parts: list[str] = []
    for chunk in value.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def _register(
    defs: list[WorkspaceDef],
    by_name: dict[str, Path],
    by_path: dict[Path, str],
    name: str,
    raw_path: str,
    *,
    source: str,
) -> None:
    """Validate one ``name -> path`` declaration and append it, or warn-and-skip. Never raises."""
    name = name.strip()
    raw_path = (raw_path or "").strip()
    if not name or not raw_path:
        _warn(f"ignoring malformed {source} entry {name or '?'}={raw_path or '?'} (want name=/abs/path)")
        return
    if name == DEFAULT_WORKSPACE:
        _warn(f"ignoring workspace 'default' from {source}: it is owned by MARSHAL_REPO")
        return
    if not _NAME_RE.match(name):
        _warn(f"ignoring workspace {name!r} from {source}: name must be letters/digits/._-")
        return
    if name in by_name:
        _warn(f"ignoring duplicate workspace name {name!r} (from {source})")
        return
    path = Path(raw_path).expanduser().resolve()
    if path in by_path:
        _warn(f"workspace {name!r} resolves to the same repo as {by_path[path]!r}; ignoring it")
        return
    defs.append(WorkspaceDef(name, path, path / "fleet.config.yaml"))
    by_name[name] = path
    by_path[path] = name


def resolve_workspaces(environ: Mapping[str, str] | None = None) -> list[WorkspaceDef]:
    """Resolve all workspace definitions (default + registry file + env). Total + crash-proof.

    The default is always present. Every path is expanduser'd + resolved; malformed / reserved /
    duplicate entries are skipped with a stderr warning. The file is the canonical registry; the env
    var adds to it (a name in both - file wins, since it is processed first).
    """
    env = os.environ if environ is None else environ
    default_repo = Path(env.get("MARSHAL_REPO", ".")).expanduser().resolve()
    default_cfg = (
        Path(env["MARSHAL_CONFIG"]).expanduser().resolve()
        if env.get("MARSHAL_CONFIG")
        else default_repo / "fleet.config.yaml"
    )
    defs: list[WorkspaceDef] = [WorkspaceDef(DEFAULT_WORKSPACE, default_repo, default_cfg)]
    by_name: dict[str, Path] = {DEFAULT_WORKSPACE: default_repo}
    by_path: dict[Path, str] = {default_repo: DEFAULT_WORKSPACE}

    file_ws, _ = read_workspaces_file(workspaces_file_path(env))
    for name, raw in file_ws.items():
        _register(defs, by_name, by_path, name, raw, source="workspaces file")

    for entry in _split_entries(env.get("MARSHAL_WORKSPACES", "")):
        name, sep, raw = entry.partition("=")  # split on the FIRST '=' only (paths may contain '=')
        if not sep:
            _warn(f"ignoring malformed MARSHAL_WORKSPACES entry {entry!r} (want name=/abs/path)")
            continue
        _register(defs, by_name, by_path, name, raw, source="MARSHAL_WORKSPACES")
    return defs


def resolve_run_gate(
    defs: list[WorkspaceDef],
    environ: Mapping[str, str] | None = None,
    *,
    file_max: int | None = None,
    file_exists: bool = False,
) -> threading.Semaphore | None:
    """The process-wide concurrent-run cap (or None for uncapped).

    Precedence: ``MARSHAL_MAX_CONCURRENT`` > the file's ``max_concurrent`` > a default that kicks in
    whenever multi-repo is in play (more than one workspace, or a registry file exists). A lone
    default workspace with no file stays uncapped, preserving today's single-repo behavior.
    """
    env = os.environ if environ is None else environ
    cap: int | None = None
    raw = env.get("MARSHAL_MAX_CONCURRENT")
    if raw:
        try:
            cap = int(raw)
        except ValueError:
            _warn(f"MARSHAL_MAX_CONCURRENT={raw!r} is not an integer; ignoring")
            cap = None
        else:
            if cap <= 0:
                _warn(f"MARSHAL_MAX_CONCURRENT={cap} must be positive; ignoring")
                cap = None
    if cap is None:
        cap = file_max
    if cap:
        return threading.BoundedSemaphore(cap)
    if len(defs) > 1 or file_exists:
        return threading.BoundedSemaphore(DEFAULT_MAX_CONCURRENT)
    return None


def build_service_for(
    wdef: WorkspaceDef, *, run_gate: threading.Semaphore | None = None
) -> MarshalService:
    """Build the single-repo MarshalService for one workspace (mirrors the legacy build_service).

    A workspace whose config file is absent still builds, with zero clients, so a registered-but-
    unconfigured repo degrades gracefully instead of raising on first use (the never-crash grace).
    A malformed config still raises here - same as the single-repo path has always done.
    """
    if not wdef.config_path.exists():
        _warn(
            f"workspace {wdef.name!r}: no fleet config at {wdef.config_path}; starting with zero "
            "clients. Add one with `marshal workspace add` --scaffold, copy fleet.config.example.yaml, "
            "or set MARSHAL_CONFIG (default workspace). See SETUP.md."
        )
        return MarshalService(
            wdef.path, FleetConfig(), config_path=wdef.config_path, run_gate=run_gate
        )
    config = load_config(wdef.config_path)
    for warning in validate(config):
        _warn(f"workspace {wdef.name!r} config warning: {warning}")
    return MarshalService(wdef.path, config, config_path=wdef.config_path, run_gate=run_gate)


class WorkspaceRegistry:
    """Lazily builds + caches one MarshalService per workspace; resolves run_ids to their owner.

    Build is memoized only on SUCCESS (a transient failure never poisons a workspace) behind a
    per-workspace lock. When constructed with a ``resolver`` (the ``from_env`` path), it hot-reloads:
    each operation re-resolves the declarations and ADDS any new workspaces, leaving cached services
    untouched - so a freshly registered repo is usable without reconnecting the server.
    """

    def __init__(
        self,
        defs: list[WorkspaceDef],
        *,
        run_gate: threading.Semaphore | None = None,
        builder: Callable[[WorkspaceDef], MarshalService] | None = None,
        prebuilt: Mapping[str, MarshalService] | None = None,
        resolver: Callable[[], list[WorkspaceDef]] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if not defs:
            raise ValueError("a workspace registry needs at least the default workspace")
        self._defs: dict[str, WorkspaceDef] = {}
        for d in defs:
            self._defs.setdefault(d.name, d)
        self._run_gate = run_gate
        self._builder = builder or (lambda d: build_service_for(d, run_gate=run_gate))
        self._cache: dict[str, MarshalService] = dict(prebuilt or {})
        self._locks: dict[str, threading.Lock] = {name: threading.Lock() for name in self._defs}
        self._resolver = resolver
        # The env this registry resolves against, so `add()` writes the SAME file the resolver reads.
        self._environ = environ
        self._defs_lock = threading.Lock()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WorkspaceRegistry:
        env = os.environ if environ is None else environ
        fpath = workspaces_file_path(env)
        _, file_max = read_workspaces_file(fpath)
        defs = resolve_workspaces(env)
        gate = resolve_run_gate(defs, env, file_max=file_max, file_exists=fpath.exists())
        return cls(defs, run_gate=gate, resolver=lambda: resolve_workspaces(env), environ=env)

    @classmethod
    def for_service(
        cls, service: MarshalService, name: str = DEFAULT_WORKSPACE
    ) -> WorkspaceRegistry:
        """Wrap a single prebuilt service as a one-workspace registry (the single-repo / test path)."""
        wdef = WorkspaceDef(name, Path(service.repo_root).resolve(), Path(service.config_path))
        return cls([wdef], prebuilt={name: service})

    @property
    def run_gate(self) -> threading.Semaphore | None:
        return self._run_gate

    def _refresh(self) -> None:
        """Re-resolve declarations and ADD any new workspaces (hot-reload). No-op when static.

        Additive only: existing names (and their cached services) are left as-is - changing or
        removing a workspace needs a reconnect. A resolver failure keeps the current registry.
        """
        if self._resolver is None:
            return
        try:
            fresh = self._resolver()
        except Exception as exc:  # noqa: BLE001 - a bad file must never break a live registry
            _warn(f"workspace refresh failed; keeping the current registry: {exc}")
            return
        new = [d for d in fresh if d.name not in self._defs]
        if not new:
            return
        with self._defs_lock:
            merged = dict(self._defs)
            for d in new:
                if d.name not in merged:
                    merged[d.name] = d
                    self._locks.setdefault(d.name, threading.Lock())
            self._defs = merged  # atomic rebind; readers see the old or new dict, never a torn one

    def names(self) -> list[str]:
        self._refresh()
        return list(self._defs)

    def get(self, name: str | None = None) -> MarshalService:
        """Return the workspace's service, building (and caching) it on first touch."""
        self._refresh()
        wsname = name or DEFAULT_WORKSPACE
        wdef = self._defs.get(wsname)
        if wdef is None:
            raise ValueError(f"unknown workspace {wsname!r}; known: {', '.join(self.names())}")
        svc = self._cache.get(wsname)
        if svc is not None:
            return svc
        with self._locks[wsname]:
            svc = self._cache.get(wsname)
            if svc is None:
                svc = self._builder(wdef)  # may raise; failures are NOT cached (retryable)
                self._cache[wsname] = svc
            return svc

    def add(self, name: str, path: Path | str) -> WorkspaceDef:
        """Register a workspace into the file THIS registry reads (so it hot-reloads in), then
        refresh so it is immediately resolvable. Writes against the registry's own env."""
        wdef = register_workspace(name, path, environ=self._environ)
        self._refresh()
        return wdef

    def _runs_dir(self, wdef: WorkspaceDef) -> Path:
        # The service-free scan assumes the standard ledger layout (<repo>/.marshal/runs) - the one
        # every MCP entry point produces (build_service_for never overrides Fleet's base_dir). A
        # library caller wrapping a service built with a custom base_dir would break this scan; the
        # MCP server never does, so the registry sources the path here rather than from a service it
        # may not have built yet (the cold-scan path has no service).
        return wdef.path / ".marshal" / "runs"

    def owner_of(self, run_id: str, hint: str | None = None) -> str | None:
        """The workspace that owns ``run_id``, or None. Cheap path stat; never builds a service.

        Tries ``hint`` first (a wrong hint just falls through to the scan), then every workspace in
        declaration order.
        """
        self._refresh()
        order: list[str] = []
        if hint and hint in self._defs:
            order.append(hint)
        order.extend(n for n in self._defs if n != hint)
        for name in order:
            if (self._runs_dir(self._defs[name]) / f"{run_id}.json").exists():
                return name
        return None

    def resolve_run(
        self, run_id: str, hint: str | None = None
    ) -> tuple[str, MarshalService] | None:
        """Locate a run's owning (name, service), building only the owner. None if no one owns it."""
        owner = self.owner_of(run_id, hint)
        if owner is None:
            return None
        return owner, self.get(owner)

    def require_run(self, run_id: str, hint: str | None = None) -> tuple[str, MarshalService]:
        """resolve_run, but raise a clear error (listing valid workspaces) if no one owns the id."""
        resolved = self.resolve_run(run_id, hint)
        if resolved is None:
            raise ValueError(
                f"no run {run_id!r} in any registered workspace: {', '.join(self.names())}"
            )
        return resolved

    def ledger_runs(self, name: str | None = None) -> list[tuple[str, RunRecord]]:
        """Every recorded run tagged with its workspace, read straight from the ledgers (no build).

        ``name`` scopes to one workspace; None aggregates across all registered workspaces - so a run
        spawned in a non-default workspace is still visible to a bare ``status()``.
        """
        self._refresh()
        if name is not None and name not in self._defs:
            raise ValueError(f"unknown workspace {name!r}; known: {', '.join(self.names())}")
        targets = [name] if name else list(self._defs)
        out: list[tuple[str, RunRecord]] = []
        for wsname in targets:
            for rec in FleetState(self._runs_dir(self._defs[wsname])).list():
                out.append((wsname, rec))
        return out

    def describe(self) -> list[dict[str, Any]]:
        """list_workspaces payload: name, path, config_path, configured?, client_count, default?.

        Reads each config to count declared clients (no subprocess, no service build); a broken
        config reports 0 clients rather than raising.
        """
        self._refresh()
        rows: list[dict[str, Any]] = []
        for name, wdef in self._defs.items():
            configured = wdef.config_path.exists()
            client_count = 0
            if configured:
                try:
                    client_count = len(load_config(wdef.config_path).clients)
                except ConfigError:
                    client_count = 0
            rows.append(
                {
                    "name": name,
                    "path": str(wdef.path),
                    "config_path": str(wdef.config_path),
                    "configured": configured,
                    "client_count": client_count,
                    "default": name == DEFAULT_WORKSPACE,
                }
            )
        return rows
