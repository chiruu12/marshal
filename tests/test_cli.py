"""Tests for the marshal CLI --json output paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marshal_engine import cli


def test_backends_json(capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["backends", "--json"])
    assert ret == 0
    out, _ = capsys.readouterr()
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) >= 1
    for item in data:
        assert set(item.keys()) == {
            "name",
            "available",
            "json_output",
            "native_usage",
            "permission_modes",
        }
        assert isinstance(item["available"], bool)
        assert isinstance(item["json_output"], bool)
        assert isinstance(item["native_usage"], bool)
        assert isinstance(item["permission_modes"], list)


def test_backends_human(capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["backends"])
    assert ret == 0
    out, _ = capsys.readouterr()
    assert "available=" in out
    assert "json=" in out


def test_usage_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["usage", "--json", "--dir", str(tmp_path / "usage")])
    assert ret == 0
    out, _ = capsys.readouterr()
    data = json.loads(out)
    assert isinstance(data, dict)
    assert "totals" in data
    assert "by_backend" in data
    assert "by_client" in data
    assert "by_model" in data


def test_usage_human_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["usage", "--dir", str(tmp_path / "usage")])
    assert ret == 0
    out, _ = capsys.readouterr()
    assert "runs=0" in out


def test_status_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["status", "--json", "--state", str(tmp_path / "runs")])
    assert ret == 0
    out, _ = capsys.readouterr()
    data = json.loads(out)
    assert isinstance(data, list)
    assert data == []


def test_status_human_no_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["status", "--state", str(tmp_path / "runs")])
    assert ret == 0
    out, _ = capsys.readouterr()
    assert "no runs recorded" in out


def test_doctor_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["doctor", "--json", "--repo", str(tmp_path), "--config", str(tmp_path / "none.yaml")])
    assert ret in (0, 1)  # exit code tracks hard failures; structure is what we assert here
    data = json.loads(capsys.readouterr()[0])
    assert set(data) == {"checks", "fails", "warns"}
    assert isinstance(data["checks"], list) and data["checks"]
    for c in data["checks"]:
        assert set(c.keys()) == {"name", "status", "detail", "fix"}
    assert "python" in {c["name"] for c in data["checks"]}


_FLEET = "clients:\n  a:\n    backend: cursor\n  b:\n    backend: cursor\n"


def _repo_with_workflow(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "workflows").mkdir(parents=True)
    (repo / "fleet.config.yaml").write_text(_FLEET)
    (repo / "workflows" / "review.yaml").write_text(body)
    return repo


_VALID = "name: review\ninputs: [target]\nphases:\n  - run: fan_out\n    clients: [a, b]\n    goal: 'check {target}'\n  - run: collect\n"
_BAD_CLIENT = "name: review\nphases:\n  - run: fan_out\n    clients: [ghost]\n    goal: g\n"


def test_workflows_json_valid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo_with_workflow(tmp_path, _VALID)
    ret = cli.main(["workflows", "--repo", str(repo), "--json"])
    assert ret == 0
    data = json.loads(capsys.readouterr()[0])
    assert data[0]["name"] == "review"
    assert data[0]["error"] is None
    assert [p["run"] for p in data[0]["phases"]] == ["fan_out", "collect"]


def test_workflows_validates_client_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo_with_workflow(tmp_path, _BAD_CLIENT)
    ret = cli.main(["workflows", "--repo", str(repo)])
    assert ret == 1  # an invalid recipe makes the command fail (fail-fast for CI)
    out, _ = capsys.readouterr()
    assert "unknown client" in out


def test_workflows_none_present(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ret = cli.main(["workflows", "--repo", str(repo)])
    assert ret == 0
    assert "no workflows" in capsys.readouterr()[0]


# --- workspace registry subcommand ----------------------------------------------------------


def test_workspace_add_list_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.setenv("MARSHAL_REPO", str(tmp_path))
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(tmp_path / "w.yaml"))

    assert cli.main(["workspace", "add", "alpha", str(repo)]) == 0
    assert (repo / "fleet.config.yaml").exists()  # scaffolded by default
    assert "registered workspace 'alpha'" in capsys.readouterr()[0]

    assert cli.main(["workspace", "list"]) == 0
    out = capsys.readouterr()[0]
    assert "alpha" in out and "default" in out

    assert cli.main(["workspace", "remove", "alpha"]) == 0
    assert "removed workspace 'alpha'" in capsys.readouterr()[0]
    assert cli.main(["workspace", "remove", "alpha"]) == 1  # already gone -> nonzero
    assert "no workspace" in capsys.readouterr()[0]


def test_workspace_add_json_and_no_scaffold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.setenv("MARSHAL_REPO", str(tmp_path))
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(tmp_path / "w.yaml"))

    assert cli.main(["workspace", "add", "alpha", str(repo), "--no-scaffold", "--json"]) == 0
    data = json.loads(capsys.readouterr()[0])
    assert data["name"] == "alpha" and data["scaffolded"] is False
    assert not (repo / "fleet.config.yaml").exists()  # --no-scaffold honored


def test_workspace_bare_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MARSHAL_REPO", str(tmp_path))
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(tmp_path / "w.yaml"))
    assert cli.main(["workspace"]) == 0  # no subcommand -> lists, must not crash
    assert "default" in capsys.readouterr()[0]
