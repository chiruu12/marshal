---
name: security-hardening
description: |
  Apply concrete security hardening to a codebase. Covers input validation, auth,
  secrets management, dependency audit, and common vulnerabilities. Use when doing
  security review, hardening before launch, after a security incident, or when user
  mentions "security", "OWASP", "vulnerability", or "harden".
---

# Security Hardening

Apply concrete, actionable security practices — not vague advice. Every item here is a specific check with a specific fix. Security isn't a phase; it's a property of every line you write.

## When to Use

- Before launching to production
- After a security incident or finding
- During code review for security-sensitive code (auth, payments, PII)
- Regular security audit cadence
- **NOT** for threat modeling (use a dedicated threat model process)
- **NOT** for compliance documentation (this produces code changes, not docs)

## Process

### 1. Input validation at system boundaries

Every input from outside the system must be validated before use:

```
- HTTP request bodies, query params, headers
- File uploads (type, size, content)
- Environment variables at startup
- External API responses
- Database query results (when schema could drift)
```

**Rules:**
- Validate at the boundary, trust internally
- Reject invalid input early (fail fast)
- Use allowlists, not denylists
- Parameterize all SQL — never concatenate user input

### 2. Authentication and secrets

| Always Do | Never Do |
|-----------|----------|
| Hash passwords with bcrypt/scrypt/argon2 (cost factor ≥ 12) | Store passwords in plaintext or reversible encryption |
| Use httpOnly, secure, sameSite cookies | Store tokens in localStorage |
| Rotate secrets on suspected compromise | Hardcode secrets in source code |
| Use environment variables or secret managers | Commit .env files or credentials |
| Implement rate limiting on auth endpoints | Allow unlimited login attempts |

### 3. Output encoding

- HTML: encode before rendering (prevents XSS)
- SQL: use parameterized queries (prevents SQLi)
- Shell: never pass user input to shell commands
- URLs: encode path and query parameters

### 4. Headers and transport

```
Content-Security-Policy: default-src 'self'
Strict-Transport-Security: max-age=31536000; includeSubDomains
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
```

### 5. Dependencies

```bash
# Check for known vulnerabilities
npm audit          # Node.js
pip audit          # Python
cargo audit        # Rust
```

- Update dependencies with known CVEs immediately
- Pin major versions to prevent supply chain attacks
- Review new dependencies before adding (check maintainership, download count, last update)

### 6. Logging and error handling

- Never log passwords, tokens, PII, or full credit card numbers
- Don't expose stack traces to users in production
- Log authentication failures with enough context to detect attacks
- Implement structured logging for security events

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "It's just an internal tool, security doesn't matter" | Internal tools get exposed. Credentials get reused. Attackers pivot through internal systems. Internal ≠ safe. |
| "We'll add security later before launch" | Security is architectural. Bolting it on later means redesigning auth flows, rewriting database queries, and retrofitting validation. It's 10x more expensive after the fact. |
| "This input comes from our own frontend, it's safe" | Frontends can be bypassed. Anyone can send arbitrary HTTP requests. Server-side validation is the only validation that counts. |
| "The framework handles security for us" | Frameworks provide tools, not guarantees. You still need to use them correctly — enable CSRF protection, configure CORS, use parameterized queries. Default isn't always secure. |

## Red Flags

- User input concatenated directly into SQL queries — SQL injection
- `dangerouslySetInnerHTML` or equivalent without sanitization — XSS
- Secrets in source code or git history — credential exposure
- No rate limiting on authentication endpoints — brute force vulnerability
- `eval()` or shell exec with user input — code injection

## Verification Checklist

- [ ] All user inputs validated at system boundary (not just client-side)
- [ ] Passwords hashed with bcrypt/scrypt/argon2 (cost ≥ 12)
- [ ] No secrets in source code or git history
- [ ] SQL queries use parameterization (no string concatenation)
- [ ] Security headers configured (CSP, HSTS, X-Content-Type-Options)
- [ ] `npm audit` / equivalent shows no high/critical vulnerabilities
- [ ] Auth endpoints have rate limiting
- [ ] Error responses don't leak internal details to users

## Anti-patterns

- **DO NOT** validate only on the client — server-side validation is the real boundary
- **DO NOT** roll your own crypto — use established libraries (bcrypt, libsodium, etc.)
- **DO NOT** store secrets in code, environment variable files committed to git, or client-side storage
- **DO NOT** trust "internal" traffic — validate at every service boundary
