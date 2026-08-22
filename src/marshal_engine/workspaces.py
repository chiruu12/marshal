"""Compatibility shim: this module moved to `marshal_engine.interfaces.workspaces`.

Kept because published docs and examples import `marshal_engine.workspaces` directly. New code
should import from the new path; this re-export binds the same objects, not copies.
"""

from .interfaces.workspaces import *  # noqa: F403
