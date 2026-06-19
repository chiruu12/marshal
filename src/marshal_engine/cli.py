"""The `marshal` CLI entry point. Minimal for now — fleshed out in a later phase."""

from __future__ import annotations

import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if args and args[0] in {"-v", "--version", "version"}:
        print(f"marshal {__version__}")
        return 0

    print(f"marshal {__version__} — orchestration engine for headless coding agents")
    print("early scaffold; see docs/design.md for the architecture and roadmap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
