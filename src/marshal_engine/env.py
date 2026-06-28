"""Environment hygiene for subprocesses Marshal spawns (agents + worktree setup).

The driver typically runs inside its own activated virtualenv, so ``os.environ`` carries
``VIRTUAL_ENV`` (and sometimes ``PYTHONHOME``) pointing at the DRIVER's interpreter. Every agent
runs in an isolated worktree with its own ``.venv``; if a child inherits the driver's
``VIRTUAL_ENV``, tools like ``uv run`` / ``python`` resolve to the driver's environment instead of
the worktree's - so an agent's ``uv run pytest`` silently tests the driver's installed code, not the
worktree's edits. Stripping those vars makes each child resolve its own worktree environment.
"""

from __future__ import annotations

import os

# Vars that pin a child to the driver's Python install; cleared so the worktree's own environment
# (its `.venv`) wins. PATH is intentionally NOT touched - uv/git/the backend CLIs need it.
LEAKY_VENV_VARS = ("VIRTUAL_ENV", "PYTHONHOME")


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """``os.environ`` minus the driver's venv pins, with ``extra`` layered on top.

    ``extra`` wins, so a caller that genuinely wants to set ``VIRTUAL_ENV`` for a child still can.
    """
    env = {k: v for k, v in os.environ.items() if k not in LEAKY_VENV_VARS}
    if extra:
        env.update(extra)
    return env
