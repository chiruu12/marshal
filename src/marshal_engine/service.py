"""Compatibility shim: this module moved to `marshal_engine.interfaces.service`.

Kept because published docs and examples import `marshal_engine.service` directly. New code
should import from the new path; this re-export binds the same objects, not copies.
"""

from .interfaces.service import *  # noqa: F403
