## Summary

<!-- One line describing WHAT this change ships (not how, not iteration history). -->

## Details

<!-- What changed and why, if not obvious from the summary. -->

## Checklist

- [ ] Ran the gate locally: `uv run pytest -q && uv run ruff check src tests && uv run mypy`
- [ ] Updated docs (`README.md` / `docs/` / `CLAUDE.md`) if behavior changed
- [ ] Added a `## [Unreleased]` entry to `CHANGELOG.md`
- [ ] No internal process / planning notes / cost figures in commits, PR text, or docs
- [ ] **New backend?** Added contract tests for `build_invocation` and `map_permission`, and
      registered the factory in `registry.py`
