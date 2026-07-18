"""Docs-sync invariant tests — MCP tools, CLI subcommands, and config example stay aligned with code."""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

import marshal_engine
from marshal_engine.config import FleetConfig

_REPO_ROOT = Path(marshal_engine.__file__).resolve().parents[2]
_USAGE_MD = _REPO_ROOT / "docs" / "usage.md"
_MCP_TOOLS_MD = _REPO_ROOT / "docs" / "mcp-tools.md"
_FLEET_EXAMPLE = _REPO_ROOT / "fleet.config.example.yaml"

_CONFIG = """
clients:
  reviewer:
    backend: cursor
    permission: read-only
"""


def _repo_with_config(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fleet.config.yaml").write_text(_CONFIG)
    return repo


def _mcp_tool_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    pytest.importorskip("mcp")
    from marshal_engine.mcp_server import build_app, build_service

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    return {t.name for t in asyncio.run(app.list_tools())}


def _marshal_root_parser() -> argparse.ArgumentParser:
    """Return the root ``ArgumentParser`` that ``cli.main`` constructs (no subprocess)."""
    from marshal_engine import cli as cli_module

    captured: list[argparse.ArgumentParser] = []
    real_init = cli_module.argparse.ArgumentParser.__init__

    def _patched_init(self: argparse.ArgumentParser, *args: object, **kwargs: object) -> None:
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]
        prog = kwargs.get("prog")
        if prog is None and args:
            prog = args[0]
        if prog == "marshal":
            captured.append(self)

    with patch.object(cli_module.argparse.ArgumentParser, "__init__", _patched_init):
        cli_module.main(["--version"])
    assert captured, "marshal CLI root parser was not constructed"
    return captured[0]


def _iter_subparsers(
    parser: argparse.ArgumentParser, parent: str = ""
) -> Iterator[tuple[str, str]]:
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            yield parent, name
            yield from _iter_subparsers(sub, name)


def _usage_cli_section(text: str) -> str:
    start = text.index("## Use it as a CLI")
    rest = text[start + 1 :]
    next_h2 = rest.find("\n## ")
    return text[start:] if next_h2 < 0 else text[start : start + 1 + next_h2]


def _usage_mcp_section(text: str) -> str:
    start = text.index("## Use it as an MCP server")
    end = text.index("## Use it as a CLI")
    return text[start:end]


def _doc_tool_names_usage(section: str) -> set[str]:
    return set(re.findall(r"^\| `([a-z_][a-z0-9_]*)", section, flags=re.MULTILINE))


def _doc_tool_names_mcp_tools(text: str) -> set[str]:
    return set(re.findall(r"^### `([a-z_][a-z0-9_]*)`", text, flags=re.MULTILINE))


def _assert_names_in_text(
    *,
    names: set[str],
    text: str,
    doc_label: str,
    direction: str,
) -> None:
    missing = sorted(name for name in names if name not in text)
    assert not missing, f"{doc_label} {direction}: {', '.join(missing)}"


def test_mcp_tools_match_usage_and_mcp_tools_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _mcp_tool_names(tmp_path, monkeypatch)
    usage_text = _USAGE_MD.read_text(encoding="utf-8")
    mcp_tools_text = _MCP_TOOLS_MD.read_text(encoding="utf-8")
    usage_section = _usage_mcp_section(usage_text)

    _assert_names_in_text(
        names=tools,
        text=usage_section,
        doc_label="docs/usage.md (MCP tool table)",
        direction="missing tool names",
    )
    _assert_names_in_text(
        names=tools,
        text=mcp_tools_text,
        doc_label="docs/mcp-tools.md",
        direction="missing tool names",
    )

    usage_doc_tools = _doc_tool_names_usage(usage_section)
    mcp_doc_tools = _doc_tool_names_mcp_tools(mcp_tools_text)
    stale_usage = sorted(usage_doc_tools - tools)
    stale_mcp = sorted(mcp_doc_tools - tools)
    assert not stale_usage, (
        "docs/usage.md (MCP tool table) documents removed tools: "
        + ", ".join(stale_usage)
    )
    assert not stale_mcp, (
        "docs/mcp-tools.md documents removed tools: " + ", ".join(stale_mcp)
    )


def test_cli_subcommands_documented_in_usage() -> None:
    usage_cli = _usage_cli_section(_USAGE_MD.read_text(encoding="utf-8"))
    for parent, name in _iter_subparsers(_marshal_root_parser()):
        if parent:
            needle = f"marshal {parent} {name}"
        else:
            needle = f"marshal {name}"
        assert needle in usage_cli, f"docs/usage.md (CLI section) missing subcommand: {needle}"


def test_fleet_config_example_mentions_every_top_level_field() -> None:
    example_text = _FLEET_EXAMPLE.read_text(encoding="utf-8")
    fields = set(FleetConfig.model_fields)
    missing = sorted(field for field in fields if field not in example_text)
    assert not missing, (
        "fleet.config.example.yaml missing FleetConfig field names: " + ", ".join(missing)
    )
