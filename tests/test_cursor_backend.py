"""Contract tests for CursorBackend (pure hooks + JSON parse; no spawning/network)."""

from __future__ import annotations

import json
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
