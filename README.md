# Claude Skills Template

A GitHub template with **59 curated Claude Code skills** for bootstrapping new projects. Skip the setup, start building.

## Quick Start

1. Click **"Use this template"** on GitHub
2. Clone your new repo
3. Edit `CLAUDE.md` — fill in your project details
4. Start using skills: `/tdd`, `/gstack-ship`, `/grill-me`, etc.

## What's Included

### `.claude/settings.json`
- Read-only permissions pre-allowed (Read, Glob, Grep, git status/log/diff)
- Destructive operations blocked (force-push, hard reset, rm -rf)
- Pre-tool-use safety hook that catches dangerous git commands

### `.claude/skills/` — 59 Skills

#### Core Development (6)
| Skill | What it does |
|---|---|
| `/tdd` | Test-driven development with red-green-refactor loop |
| `/incremental-implementation` | Build features in small, always-compilable increments |
| `/spec-driven-development` | Write a spec before coding |
| `/context-engineering` | Optimize AI agent context for effectiveness |
| `/design-an-interface` | Generate multiple interface designs ("Design It Twice") |
| `/write-a-skill` | Create new Claude Code skills |

#### Planning & Architecture (8)
| Skill | What it does |
|---|---|
| `/grill-me` | Stress-test a plan through relentless questioning |
| `/zoom-out` | Step back and map the broader system context |
| `/improve-codebase-architecture` | Find deepening opportunities in a codebase |
| `/domain-model` | DDD domain modeling |
| `/ubiquitous-language` | Extract domain glossary |
| `/request-refactor-plan` | Plan safe refactors with tiny commits |
| `/to-prd` | Convert conversation context into a PRD |
| `/to-issues` | Break plans into independently-grabbable GitHub issues |

#### Code Quality & Safety (5)
| Skill | What it does |
|---|---|
| `/security-hardening` | OWASP-informed security review and hardening |
| `/performance-optimization` | Systematic measure-identify-fix-verify optimization |
| `/deprecation-migration` | Plan and execute deprecation/migration paths |
| `/git-guardrails-claude-code` | Set up Husky hooks to block dangerous git commands |
| `/setup-pre-commit` | Set up Husky + lint-staged + Prettier |

#### Workflow (6)
| Skill | What it does |
|---|---|
| `/triage-issue` | Triage bugs, find root cause, create GitHub issues |
| `/github-triage` | Label-based GitHub issue state machine |
| `/pr-triage` | Triage AI bot review comments and CI failures on PRs |
| `/qa` | Interactive QA session with issue filing |
| `/edit-article` | Edit articles by restructuring and rewriting |
| `/caveman` | Ultra-compressed communication mode (~75% fewer tokens) |

#### gstack: Ship & Review (4)
| Skill | What it does |
|---|---|
| `/gstack-ship` | Ship workflow: test, review, version bump, PR |
| `/gstack-land-and-deploy` | Merge PR, wait for CI, verify production |
| `/gstack-review` | Pre-landing PR review for SQL safety, trust boundaries |
| `/gstack-autoplan` | Auto-run the full CEO + design + eng review pipeline |

#### gstack: Plan Review (5)
| Skill | What it does |
|---|---|
| `/gstack-plan-ceo-review` | CEO/founder-mode plan review — think bigger |
| `/gstack-plan-eng-review` | Eng manager review — architecture and edge cases |
| `/gstack-plan-design-review` | Designer's eye plan review |
| `/gstack-plan-devex-review` | Developer experience plan review |
| `/gstack-office-hours` | YC-style office hours for brainstorming ideas |

#### gstack: Design (4)
| Skill | What it does |
|---|---|
| `/gstack-design-consultation` | Full design system creation (DESIGN.md) |
| `/gstack-design-shotgun` | Generate multiple design variants for comparison |
| `/gstack-design-html` | Production-quality HTML/CSS from designs |
| `/gstack-design-review` | Visual QA with iterative fixes |

#### gstack: QA & Testing (5)
| Skill | What it does |
|---|---|
| `/gstack-qa` | Systematic QA testing with fix loop |
| `/gstack-qa-only` | QA report only — no fixes |
| `/gstack-browse` | Headless browser for testing and screenshots |
| `/gstack-benchmark` | Performance regression detection |
| `/gstack-canary` | Post-deploy canary monitoring |

#### gstack: Debugging & Safety (6)
| Skill | What it does |
|---|---|
| `/gstack-investigate` | Root cause debugging (no fixes without root cause) |
| `/gstack-careful` | Warn before destructive commands |
| `/gstack-freeze` | Restrict edits to a specific directory |
| `/gstack-unfreeze` | Clear freeze boundary |
| `/gstack-guard` | Full safety mode (careful + freeze) |
| `/gstack-cso` | Infrastructure-first security audit |

#### gstack: Productivity (10)
| Skill | What it does |
|---|---|
| `/gstack-context-save` | Save working context for later |
| `/gstack-context-restore` | Restore saved context |
| `/gstack-health` | Code quality dashboard (0-10 score) |
| `/gstack-learn` | Manage project learnings across sessions |
| `/gstack-retro` | Weekly engineering retrospective |
| `/gstack-document-release` | Post-ship documentation updates |
| `/gstack-setup-deploy` | Configure deployment settings |
| `/gstack-make-pdf` | Markdown to publication-quality PDF |
| `/gstack-codex` | OpenAI Codex CLI wrapper for second opinions |
| `/gstack-devex-review` | Live developer experience audit |

## Customizing

**Remove a category** — delete by prefix:
```bash
rm -rf .claude/skills/gstack-*     # remove all gstack skills
rm -rf .claude/skills/gstack-design-*  # remove just design skills
```

**Add your own skills** — create a directory in `.claude/skills/` with a `SKILL.md`:
```yaml
---
name: my-skill
description: |
  What it does and when to use it.
---

Your skill instructions here.
```

**Adjust permissions** — edit `.claude/settings.json` to allow/deny additional operations.

## Attribution

Skills in this template come from:
- [mattpocock/skills](https://github.com/mattpocock/skills) (MIT)
- [garrytan/gstack](https://github.com/garrytan/gstack) (MIT)

## License

MIT — see [LICENSE](LICENSE).
