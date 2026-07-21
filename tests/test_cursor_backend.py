"""Contract tests for CursorBackend: pure hooks + JSON parse, plus the safe-edit
``.cursor/cli.json`` transaction lifecycle (run-level tests spawn a local fake
``cursor-agent`` - still no network, no real Cursor install)."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec
from marshal_engine.backends.cursor import SAFE_EDIT_DENY, CursorBackend, _parse_about


@pytest.fixture
def backend() -> CursorBackend:
    return CursorBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


# The fake agent verifies the deny overlay is LIVE while the process runs (exit 1 if any
# curated deny is missing), performs a test-specific action, then emits Cursor's result JSON.
_FAKE_CURSOR = """\
import json
import sys
from pathlib import Path

cfg = Path(".cursor/cli.json")
data = json.loads(cfg.read_text(encoding="utf-8"))
deny = data.get("permissions", {}).get("deny", [])
missing = [r for r in __DENIES__ if r not in deny]
if missing:
    print("missing denies: %s" % missing, file=sys.stderr)
    sys.exit(1)
__ACTION__
print(json.dumps({"type": "result", "is_error": False, "result": __TEXT__, "session_id": "s1"}))
"""


def _fake_body(action: str = "", text: str = "done") -> str:
    return (
        _FAKE_CURSOR.replace("__DENIES__", repr(list(SAFE_EDIT_DENY)))
        .replace("__ACTION__", action)
        .replace("__TEXT__", repr(text))
    )


def test_map_permission(backend: CursorBackend) -> None:
    assert backend.map_permission(PermissionMode.READ_ONLY) == ["--mode", "plan"]
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--force"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--yolo"]


def test_prepare_safe_edit_writes_deny_list(backend: CursorBackend, tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    backend.prepare(_opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    cli = wt / ".cursor" / "cli.json"
    data = json.loads(cli.read_text(encoding="utf-8"))
    deny = data["permissions"]["deny"]
    for rule in SAFE_EDIT_DENY:
        assert rule in deny
    # curated minimum from design.md / issue #17
    assert "Shell(rm)" in deny
    assert "Write(**/.env)" in deny
    assert "Write(**/.git/**)" in deny


def test_prepare_safe_edit_merges_existing_cli_json(
    backend: CursorBackend, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    cursor_dir = wt / ".cursor"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "cli.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": ["Shell(git)"],
                    "deny": ["Shell(rm)", "WebFetch(evil.example)"],
                },
                "version": 1,
            }
        ),
        encoding="utf-8",
    )
    backend.prepare(_opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    backend.prepare(_opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))  # idempotent
    data = json.loads((cursor_dir / "cli.json").read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["permissions"]["allow"] == ["Shell(git)"]
    deny = data["permissions"]["deny"]
    assert deny.count("Shell(rm)") == 1
    assert "WebFetch(evil.example)" in deny
    for rule in SAFE_EDIT_DENY:
        assert rule in deny


def test_prepare_yolo_and_readonly_skip_cli_json(
    backend: CursorBackend, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    backend.prepare(_opts(cwd=wt, permission=PermissionMode.YOLO))
    backend.prepare(_opts(cwd=wt, permission=PermissionMode.READ_ONLY))
    assert not (wt / ".cursor" / "cli.json").exists()


def test_prepare_fails_closed_on_malformed_cli_json(
    backend: CursorBackend, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    (wt / ".cursor").mkdir(parents=True)
    original = b'{"permissions": [broken'
    (wt / ".cursor" / "cli.json").write_bytes(original)
    with pytest.raises(RuntimeError, match="not valid JSON"):
        backend.prepare(_opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert (wt / ".cursor" / "cli.json").read_bytes() == original  # byte-for-byte untouched


def test_prepare_fails_closed_on_non_object_cli_json(
    backend: CursorBackend, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    (wt / ".cursor").mkdir(parents=True)
    original = b'["Shell(rm)"]'  # valid JSON, but not an object
    (wt / ".cursor" / "cli.json").write_bytes(original)
    with pytest.raises(RuntimeError, match="not an object"):
        backend.prepare(_opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert (wt / ".cursor" / "cli.json").read_bytes() == original


# --- the safe-edit cli.json transaction (real run() through a fake cursor-agent) ---------------


def test_run_safe_edit_overlay_live_then_removed(
    backend: CursorBackend, tmp_path: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    """No prior config: denies are visible to the live process, gone after run()."""
    fake_cursor_agent(_fake_body(action="Path('out.txt').write_text('hi')"))
    wt = tmp_path / "wt"
    wt.mkdir()
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert res.status is RunStatus.SUCCEEDED, res.error  # fake exits 1 if any deny was missing
    assert (wt / "out.txt").read_text() == "hi"
    assert not (wt / ".cursor").exists()  # overlay + created dir both removed


def test_run_safe_edit_restores_existing_config_bytes_and_mode(
    backend: CursorBackend, tmp_path: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    fake_cursor_agent(_fake_body())
    wt = tmp_path / "wt"
    (wt / ".cursor").mkdir(parents=True)
    cli = wt / ".cursor" / "cli.json"
    # deliberate odd formatting a JSON re-serialize would never reproduce
    original = b'{ "permissions":{"allow": [ "Shell(git)" ] },  "version": 1 }\n\n'
    cli.write_bytes(original)
    os.chmod(cli, 0o600)
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert res.status is RunStatus.SUCCEEDED, res.error
    assert cli.read_bytes() == original
    assert stat.S_IMODE(cli.stat().st_mode) == 0o600


def test_run_safe_edit_cleans_up_after_process_failure(
    backend: CursorBackend, tmp_path: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    fake_cursor_agent(_fake_body(action="sys.exit(2)"))
    wt = tmp_path / "wt"
    wt.mkdir()
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert res.status is RunStatus.FAILED
    assert not (wt / ".cursor").exists()  # cleanup is outcome-independent


def test_run_safe_edit_discards_agent_edit_to_config(
    backend: CursorBackend, tmp_path: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    """The config is a protected control-plane file for the run: agent edits are discarded."""
    fake_cursor_agent(_fake_body(action="cfg.write_text('{\\'hacked\\': true}')"))
    wt = tmp_path / "wt"
    (wt / ".cursor").mkdir(parents=True)
    cli = wt / ".cursor" / "cli.json"
    original = b'{"permissions": {"deny": ["WebFetch(evil.example)"]}}'
    cli.write_bytes(original)
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert res.status is RunStatus.SUCCEEDED, res.error
    assert cli.read_bytes() == original


def test_run_safe_edit_restore_failure_fails_the_run(
    backend: CursorBackend,
    tmp_path: Path,
    fake_cursor_agent: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never return success while Marshal's policy residue may still be in the worktree."""
    from marshal_engine.backends import cursor as cursor_mod

    fake_cursor_agent(_fake_body(action="Path('out.txt').write_text('hi')"))
    wt = tmp_path / "wt"
    wt.mkdir()

    def _boom(path: Path, snapshot: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cursor_mod, "_restore_cli_json", _boom)
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert res.status is RunStatus.FAILED
    assert "failed to restore" in (res.error or "")
    assert "disk full" in (res.error or "")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_run_safe_edit_unreadable_config_fails_before_launch(
    backend: CursorBackend, tmp_path: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    fake_cursor_agent(_fake_body(action="Path('ran.txt').write_text('x')"))
    wt = tmp_path / "wt"
    (wt / ".cursor").mkdir(parents=True)
    cli = wt / ".cursor" / "cli.json"
    cli.write_bytes(b"{}")
    os.chmod(cli, 0o000)
    try:
        res = backend.run(
            TaskSpec(id="t", goal="x"), _opts(cwd=wt, permission=PermissionMode.SAFE_EDIT)
        )
    finally:
        os.chmod(cli, 0o644)
    assert res.status is RunStatus.FAILED
    assert "cannot snapshot" in (res.error or "")
    assert not (wt / "ran.txt").exists()  # the agent process never launched
    assert cli.read_bytes() == b"{}"      # and the file was never touched


def test_run_safe_edit_symlink_cli_json_fails_closed(
    backend: CursorBackend, tmp_path: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    """A symlink must not be replaced with a regular file under a successful result."""
    fake_cursor_agent(_fake_body(action="Path('ran.txt').write_text('x')"))
    wt = tmp_path / "wt"
    (wt / ".cursor").mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_bytes(b'{"permissions":{"allow":["Shell(git)"]}}\n')
    cli = wt / ".cursor" / "cli.json"
    cli.symlink_to(target)
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert res.status is RunStatus.FAILED
    assert "symlink" in (res.error or "")
    assert not (wt / "ran.txt").exists()
    assert cli.is_symlink()
    assert target.read_bytes() == b'{"permissions":{"allow":["Shell(git)"]}}\n'


def test_run_safe_edit_broken_symlink_cli_json_fails_closed(
    backend: CursorBackend, tmp_path: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    fake_cursor_agent(_fake_body(action="Path('ran.txt').write_text('x')"))
    wt = tmp_path / "wt"
    (wt / ".cursor").mkdir(parents=True)
    cli = wt / ".cursor" / "cli.json"
    cli.symlink_to("missing-target.json")
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert res.status is RunStatus.FAILED
    assert "symlink" in (res.error or "")
    assert not (wt / "ran.txt").exists()
    assert cli.is_symlink() and not cli.exists()


def test_run_safe_edit_symlinked_cursor_dir_fails_closed(
    backend: CursorBackend, tmp_path: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    """Writing through a symlinked ``.cursor/`` would escape the worktree."""
    fake_cursor_agent(_fake_body(action="Path('ran.txt').write_text('x')"))
    shared = tmp_path / "shared-cursor"
    shared.mkdir()
    (shared / "rules.md").write_text("keep")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".cursor").symlink_to(shared)
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert res.status is RunStatus.FAILED
    assert "symlink" in (res.error or "")
    assert not (wt / "ran.txt").exists()
    assert not (shared / "cli.json").exists()
    assert (shared / "rules.md").read_text() == "keep"


def test_run_read_only_skips_transaction(
    backend: CursorBackend, tmp_path: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    fake_cursor_agent(
        'import json\nprint(json.dumps({"type": "result", "is_error": False, '
        '"result": "looked", "session_id": "s1"}))\n'
    )
    wt = tmp_path / "wt"
    wt.mkdir()
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt, permission=PermissionMode.READ_ONLY))
    assert res.status is RunStatus.SUCCEEDED
    assert not (wt / ".cursor").exists()


def test_build_invocation_basic(backend: CursorBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do it"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv[:5] == ["cursor-agent", "-p", "--output-format", "json", "--trust"]
    assert "--force" in argv
    assert "--workspace" in argv and "/tmp/wt" in argv
    assert argv[-1] == "do it"


def test_build_invocation_model_and_readonly(backend: CursorBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="inspect"),
        _opts(permission=PermissionMode.READ_ONLY, model="gpt-5"),
    )
    assert "--model" in argv and "gpt-5" in argv
    assert "--mode" in argv and "plan" in argv


def test_build_invocation_resume(backend: CursorBackend) -> None:
    argv = backend.build_invocation(TaskSpec(id="t1", goal="cont"), _opts(session_id="uuid-1"))
    i = argv.index("--resume")
    assert argv[i + 1] == "uuid-1"


def test_compose_prompt_uses_at_mentions(backend: CursorBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix", context_files=["a.py", "b.py"]), _opts()
    )
    assert "@a.py" in argv[-1] and "@b.py" in argv[-1]


def test_parse_output_success(backend: CursorBackend) -> None:
    out = '{"type":"result","subtype":"success","is_error":false,"result":"done","session_id":"uuid-9"}'
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "done"
    assert res.session_id == "uuid-9"


def test_parse_output_is_error_flag(backend: CursorBackend) -> None:
    out = '{"type":"result","is_error":true,"result":"nope","session_id":"u"}'
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.FAILED
    assert "nope" in (res.error or "")


def test_parse_output_nonzero_exit(backend: CursorBackend) -> None:
    res = backend.parse_output("", "stderr boom", 1)
    assert res.status is RunStatus.FAILED
    assert res.exit_code == 1


def test_parse_about_json() -> None:
    raw = '{"cliVersion":"x","model":"Composer 2.5","subscriptionTier":"Ultra","userEmail":"a@b.c"}'
    assert _parse_about(raw) == {"plan": "Ultra", "model": "Composer 2.5"}


def test_parse_about_text_fallback() -> None:
    raw = "About Cursor CLI\n\nModel               Composer 2.5\nSubscription Tier   Ultra\nShell   zsh\n"
    assert _parse_about(raw) == {"model": "Composer 2.5", "plan": "Ultra"}


def test_parse_about_text_fallback_colon_separated() -> None:
    assert _parse_about("Model: Composer 2.5\nSubscription Tier: Ultra\n") == {
        "model": "Composer 2.5",
        "plan": "Ultra",
    }


def test_parse_about_text_fallback_rejects_prefix_false_positives() -> None:
    # a loose prefix match would misread these as the real fields; full-label matching must not.
    raw = "Modeling          notes\nSubscription Tierless   other\n"
    assert _parse_about(raw) is None


def test_parse_about_empty_or_unusable() -> None:
    assert _parse_about("") is None
    assert _parse_about("   ") is None
    assert _parse_about('{"cliVersion":"x"}') is None  # JSON but no plan/model fields
