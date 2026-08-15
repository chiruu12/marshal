"""Package version from installed metadata — one source of truth for User-Agent and ``--version``.

Kept separate from ``__init__`` so accounting modules (e.g. eastrouter) can read the version
without importing the package top-level re-exports.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Read from installed package metadata so there is ONE source of truth (pyproject's
# [project].version). A hardcoded literal here silently drifts: the wheel says one version and
# `marshal --version` says another, which then lands in every bug report.
try:
    __version__ = _pkg_version("marshal")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
