"""Contract tests for the published driver Skills (``skills/<name>/SKILL.md``).

A Skill is a public surface: the model discovers it by its frontmatter and addresses it by its
folder name. These lock the entrypoint shape so a new or edited Skill can't ship malformed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import marshal_engine

_SKILLS_DIR = Path(marshal_engine.__file__).resolve().parents[2] / "skills"  # src/marshal_engine -> repo root
_SKILLS = sorted(p for p in _SKILLS_DIR.iterdir() if p.is_dir() and p.name[0] != ".")


def _split_frontmatter(md: str) -> tuple[dict[str, Any], str]:
    """Return (parsed frontmatter, body) for a SKILL.md, asserting the block is well-formed."""
    assert md.startswith("---\n"), "SKILL.md must open with a YAML frontmatter block"
    parts = md.split("---\n", 2)  # ["", frontmatter, body]
    assert len(parts) == 3, "frontmatter block is not closed with a '---' line"
    data = yaml.safe_load(parts[1])
    assert isinstance(data, dict), "frontmatter must be a YAML mapping"
    return data, parts[2]


def test_known_skills_are_present() -> None:
    names = {p.name for p in _SKILLS}
    assert {
        "marshal-orchestrate",
        "marshal-benchmark",
        "marshal-workflow",
        "marshal-review-gate",
    } <= names


@pytest.mark.parametrize("skill_dir", _SKILLS, ids=lambda p: p.name)
def test_skill_has_a_valid_entrypoint(skill_dir: Path) -> None:
    md_path = skill_dir / "SKILL.md"
    assert md_path.is_file(), f"{skill_dir.name} is missing SKILL.md"
    fm, body = _split_frontmatter(md_path.read_text(encoding="utf-8"))
    # name must match the directory, so the skill is addressable by its folder.
    assert fm.get("name") == skill_dir.name
    # description is what the model matches on to decide to invoke - present and substantive.
    desc = fm.get("description")
    assert isinstance(desc, str) and len(desc.strip()) >= 40
    # exactly one top-level H1 in the body.
    h1s = [ln for ln in body.splitlines() if ln.startswith("# ")]
    assert len(h1s) == 1, f"{skill_dir.name}: expected one H1, found {len(h1s)}"
