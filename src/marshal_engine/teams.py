"""Compatibility shim: this module moved to `marshal_engine.orchestration.teams`.

Kept because published docs and examples import `marshal_engine.teams` directly. New code
should import from the new path; this re-export binds the same objects, not copies.
"""

from .orchestration.teams import *  # noqa: F401,F403
