"""Fleet-config scaffolding - detect repo shape and drop a starter ``fleet.config.yaml``.

Kept separate from the workspace tenancy layer: scaffolding is a one-shot setup helper used by the
CLI and MCP ``add_workspace`` tool, not part of multi-repo routing or service construction.
"""

from __future__ import annotations

from pathlib import Path

_FLEET_STUB = """\
# Marshal fleet config: declare the worker clients Marshal can route tasks to. Each client pins a
# backend + model + permission. See fleet.config.example.yaml in the Marshal repo for the full
# reference (opencode-go, EastRouter, usage_api, worktree_setup, verify, context, etc.).
defaults:
  permission: safe-edit
  timeout_s: 900

# Add clients under `clients:` (an empty map = zero clients until you add one). Example - uncomment
# and edit, then run `marshal doctor` to verify the backend is available:
clients:
  # claude:
  #   backend: claude-code
  #   model: claude-sonnet-4-6
"""

# Project markers the scaffold detector recognizes: marker filename -> (label, setup command).
_PROJECT_MARKERS: dict[str, tuple[str, str]] = {
    "pyproject.toml": ("Python", "uv sync"),
    "package.json": ("Node", "npm install"),
    "go.mod": ("Go", "go mod download"),
    "Cargo.toml": ("Rust", "cargo fetch"),
}
# Dirs never scanned for nested projects (VCS/vendored/derived trees, Marshal's own state).
_SCAN_SKIP = {".git", ".venv", "node_modules", ".marshal"}
# Keep the stub short even in a many-package monorepo.
_SCAFFOLD_HINT_CAP = 3


def detect_project_markers(repo: Path) -> list[tuple[str, str]]:
    """``[(marker filename, relative dir)]`` for the repo; ``""`` = the root. Root markers win.

    Best-effort and shallow (depth <= 2, skipping VCS/vendored dirs), laddered: root markers
    short-circuit, then depth-1 dirs, then depth-2 - so a nested package (`sdk/pyproject.toml`)
    is found without walking the whole tree. The result only seeds COMMENTED suggestions in the
    scaffolded config, so a miss costs nothing.
    """
    try:
        root_hits = [(m, "") for m in _PROJECT_MARKERS if (repo / m).is_file()]
        if root_hits:
            return root_hits[:_SCAFFOLD_HINT_CAP]

        def _subdirs(parent: Path) -> list[Path]:
            return [
                p
                for p in sorted(parent.iterdir())
                if p.is_dir() and p.name not in _SCAN_SKIP and not p.name.startswith(".")
            ]

        level1 = _subdirs(repo)
        hits = [(m, d.name) for d in level1 for m in _PROJECT_MARKERS if (d / m).is_file()]
        if not hits:
            hits = [
                (m, f"{d.name}/{g.name}")
                for d in level1
                for g in _subdirs(d)
                for m in _PROJECT_MARKERS
                if (g / m).is_file()
            ]
        return hits[:_SCAFFOLD_HINT_CAP]
    except OSError:
        return []


def _render_fleet_stub(hints: list[tuple[str, str]]) -> str:
    """The starter config, plus commented repo-shape suggestions when a project was detected.

    Suggestions stay comments-only: a wrong auto-active worktree_setup would fail (and tear down)
    every run, bricking the workspace on a guess - a comment costs one uncomment. Nested projects
    get a `sh -c "cd <dir> && ..."` form because worktree_setup executes as an argv list with no
    shell at the worktree root, so a bare `cd <dir> && ...` cannot work.
    """
    if not hints:
        return _FLEET_STUB
    lines = [
        "",
        "# Detected project layout - suggested worktree_setup (uncomment + adjust; see",
        "# fleet.config.example.yaml; consider a matching `verify:` gate too):",
    ]
    for marker, rel in hints:
        label, cmd = _PROJECT_MARKERS[marker]
        if rel:
            lines.append(f"# {label} project at {rel}/ ({marker}):")
            lines.append(f'# worktree_setup: sh -c "cd {rel} && {cmd}"')
            lines.append("# allow_unsafe_commands: true  # required for sh -c forms")
        else:
            lines.append(f"# {label} project at the repo root ({marker}):")
            lines.append(f"# worktree_setup: {cmd}")
    first_nested = next((rel for _m, rel in hints if rel), "")
    if first_nested:
        lines += [
            "# Nested worktree_setup uses sh -c and needs allow_unsafe_commands: true",
            "# (see docs/config.md).",
            "# context:",
            f"#   worker: The project lives under {first_nested}/; run its tooling from that directory.",
        ]
    return _FLEET_STUB + "\n".join(lines) + "\n"


def scaffold_fleet_config(repo: Path | str) -> bool:
    """Drop a starter ``fleet.config.yaml`` into a repo that has none. Returns False if one exists.

    The stub is templated to the repo's detected shape (root vs nested project) with commented
    suggestions only - it always loads as a valid zero-client config.
    """
    cfg = Path(repo) / "fleet.config.yaml"
    if cfg.exists():
        return False
    try:
        hints = detect_project_markers(Path(repo))
    except Exception:  # noqa: BLE001 - detection is a nicety; never block the scaffold
        hints = []
    cfg.write_text(_render_fleet_stub(hints), encoding="utf-8")
    return True
