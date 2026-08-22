"""Compatibility shim: this module moved to `marshal_engine.core.config`.

Kept because published docs and examples import `marshal_engine.config` directly. New code
should import from the new path; this re-export binds the same objects, not copies.
"""

from .core.config import *  # noqa: F403
