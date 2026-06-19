---
name: setup-pre-commit
description: >
  Set up Husky pre-commit hooks with lint-staged (Prettier), type checking,
  and tests in the current repo. Use when user wants to add pre-commit hooks,
  set up Husky, configure lint-staged, or add commit-time formatting/
  typechecking/testing.
---

# Setup Pre-Commit Hooks

Installs a complete pre-commit pipeline — Husky hooks running lint-staged (Prettier), type checking, and tests. Formatting debates die when the machine enforces consistency, and catching type errors at commit time is cheaper than catching them in CI 10 minutes later.

## When to Use

- User wants to add pre-commit hooks to a JavaScript/TypeScript project
- User asks about Husky, lint-staged, or commit-time formatting
- New project needs a standard quality gate
- **NOT** for Python projects — use pre-commit framework instead
- **NOT** when project already has working Husky setup — audit/fix instead of reinstalling
- **NOT** for CI-only checks that shouldn't block local commits

## Process

### 1. Detect package manager

Check for `package-lock.json` (npm), `pnpm-lock.yaml` (pnpm), `yarn.lock` (yarn), `bun.lockb` (bun). Default to npm if unclear.

### 2. Install dependencies

```bash
husky lint-staged prettier
```

### 3. Initialize Husky

```bash
npx husky init
```

Creates `.husky/` dir and adds `prepare: "husky"` to package.json.

### 4. Create `.husky/pre-commit`

```
npx lint-staged
npm run typecheck
npm run test
```

**Adapt**: Replace `npm` with detected package manager. If repo has no `typecheck` or `test` script, omit those lines and tell the user.

### 5. Create `.lintstagedrc`

```json
{
  "*": "prettier --ignore-unknown --write"
}
```

### 6. Create `.prettierrc` (if missing)

```json
{
  "useTabs": false,
  "tabWidth": 2,
  "printWidth": 80,
  "singleQuote": false,
  "trailingComma": "es5",
  "semi": true,
  "arrowParens": "always"
}
```

### 7. Verify and commit

Stage all files and commit — the commit itself is the smoke test.

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "I'll skip the test commit, it probably works" | The commit IS the test. If Husky fails on the setup commit, you catch config issues immediately. |
| "I'll add typecheck/test hooks later" | Later never comes. Include or explicitly tell user they're missing. |
| "I'll use `--no-verify` to skip the hook this once" | Once becomes always. Better to fix the hook than skip it. |
| "Just format everything, not just staged files" | Formatting unstaged files creates noise in diffs. lint-staged scopes to staged files only. |

## Red Flags

- Project has no `package.json` (not a JS/TS project — wrong skill)
- Existing `.husky/` directory with custom hooks (could overwrite team config)
- Monorepo with multiple packages (need per-package lint-staged config)
- CI already runs these checks and team prefers fast local commits

## Verification Checklist

- [ ] `.husky/pre-commit` exists and is executable
- [ ] `.lintstagedrc` exists with valid JSON
- [ ] `package.json` contains `"prepare": "husky"`
- [ ] Prettier config exists
- [ ] `npx lint-staged` exits 0
- [ ] A test commit triggers the pre-commit hook
- [ ] If typecheck/test lines omitted, user was told why

## Anti-patterns

- **DO NOT** install globally — Husky must be a devDependency so it works for all contributors
- **DO NOT** add a shebang to Husky v9+ hook files — causes errors on some systems
- **DO NOT** format all files in pre-commit (`prettier .`) — use lint-staged for staged-only
- **DO NOT** silently skip missing scripts — always inform the user
- **DO NOT** overwrite existing Husky hooks without checking what's there
