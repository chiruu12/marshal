"""Compatibility shim: this module moved to `marshal_engine.runtime.state`.

Kept because published docs and examples import `marshal_engine.state` directly. New code
should import from the new path; this re-export binds the same objects, not copies.
"""

from .runtime.state import *  # noqa: F401,F403
