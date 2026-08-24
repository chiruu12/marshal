"""Compatibility shim: this module moved to `marshal_engine.interfaces.cli`.

Kept because an already-installed `marshal` console script has the old entry point baked in
(`marshal_engine.cli:main`) and only picks up the new one on reinstall.
"""

from .interfaces.cli import *  # noqa: F403
from .interfaces.cli import main  # noqa: F401  - the console-script entry point
