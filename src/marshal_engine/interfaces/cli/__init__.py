"""The `marshal` CLI, grouped by concern.

`parser` owns argument wiring and dispatch; the command handlers live beside it in `inspect`
(read-only views), `runs` (dispatch work), `recipes` (workflows and teams), and `admin` (setup,
workspaces, cleanup). `formatting` is the shared display layer and `common` the shared argument
and service helpers.
"""

from .parser import main

__all__ = ["main"]
