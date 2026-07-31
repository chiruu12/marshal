"""read_paths: let a run read material outside its worktree.

Declared paths are copied under <worktree>/.marshal-context/<basename> (read-only, git-excluded)
so they never appear in the run's diff. Secret-shaped names (.env*, *.pem, id_rsa*, id_ed25519*,
or anything under .ssh) are refused — including descendants of a declared directory.

Prerequisites (see examples/README.md):
  * uv sync --extra mcp --extra dev
  * a fleet.config.yaml with at least one client (copy fleet.config.example.yaml)
  * that client's backend CLI installed AND authenticated (run: uv run marshal doctor)

Run from the repo root:  uv run python examples/read_paths.py
"""

import tempfile
from pathlib import Path

from marshal_engine.core.config import load_config
from marshal_engine.interfaces.service import MarshalService

CLIENT = "implementer"



def _require_clients(config_path: Path, *names: str) -> None:
    """Fail with guidance, not a stack trace, when the config uses different client names.

    These names come from fleet.config.example.yaml, which the prerequisites tell you to copy -
    but client names are the user's own vocabulary, so a mismatch is expected and should say what
    to do about it.
    """
    declared = load_config(config_path).clients
    missing = [n for n in names if n not in declared]
    if missing:
        have = ", ".join(sorted(declared)) or "(none)"
        raise SystemExit(
            f"this example expects client(s) {', '.join(missing)} in {config_path}; "
            f"declared: {have}. Rename the constants at the top of this file, or copy the "
            f"clients from fleet.config.example.yaml."
        )

def main() -> None:
    _require_clients(Path("fleet.config.yaml"), CLIENT)
    service = MarshalService(Path("."), load_config("fleet.config.yaml"))

    with tempfile.TemporaryDirectory(prefix="marshal-read-paths-") as tmp:
        tmp_path = Path(tmp)

        # Legitimate use: a spec living outside the worktree (here: a temp file on disk).
        spec = tmp_path / "feature-spec.md"
        spec.write_text(
            "# Spec\n\nAdd a one-line comment `# read_paths demo` to README.md.\n",
            encoding="utf-8",
        )

        # Secret-shaped path name only — content is a placeholder, not a real secret.
        fake_env = tmp_path / ".env"
        fake_env.write_text("# placeholder — not a real secret\n", encoding="utf-8")

        try:
            service.run_agent(
                CLIENT,
                "This should never start; read_paths must refuse .env",
                read_paths=[str(fake_env)],
            )
        except ValueError as exc:
            print("secret-shaped path refused:", exc)
        else:
            raise SystemExit("expected ValueError for secret-shaped read_paths")

        record = service.run_agent(
            CLIENT,
            (
                "Read the spec under .marshal-context/feature-spec.md and follow it. "
                "Do not modify anything under .marshal-context/."
            ),
            read_paths=[str(spec)],
        )
        print(f"status={record.status}  read_paths={record.read_paths}")

        ctx = Path(record.worktree) / ".marshal-context"
        print("context dir:", sorted(p.name for p in ctx.iterdir()) if ctx.is_dir() else None)

        collected = service.collect_run(record.run_id)
        leaked = [f for f in collected.changed_files if ".marshal-context" in f]
        print("changed_files:", collected.changed_files)
        print("marshal-context in diff:", leaked)  # expect []


if __name__ == "__main__":
    main()
