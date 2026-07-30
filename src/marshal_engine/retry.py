"""Bounded retries for transient backend failures.

A coding-agent run can fail for reasons that have nothing to do with the task: a backend's local
state DB is momentarily locked by a sibling run, a provider returns a rate-limit / overloaded / 5xx,
or a connection drops mid-handshake. Re-running usually succeeds. This module decides (a) whether a
failed result *looks* transient and (b) how long to wait before the next attempt.

It deliberately does NOT retry:
  * timeouts (`RunStatus.TIMED_OUT`) - a retry burns another full timeout window, and
  * genuine task failures (the agent ran and produced a wrong/erroring result) - a retry just
    spends money to fail again.
The marker list is intentionally conservative: a false positive wastes a whole run.

HTTP status codes (429/502/503/504) only count when framed as transport/provider errors
(``http 429``, ``status 503``, ``error code: 429``, or a bounded code next to a known reason
phrase). A bare digit substring in task output must not trigger a retry.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from .types import AgentResult, RunStatus

# Lowercased substrings that mark an INFRASTRUCTURE / transport failure (not a task failure),
# matched against the failed result's error text. No bare HTTP status digits here — those need
# framing (see ``_TRANSIENT_STATUS_RE``) so agent output that merely mentions a code is ignored.
TRANSIENT_MARKERS: tuple[str, ...] = (
    "database is locked",       # opencode / sqlite contention from a sibling run
    "rate limit",
    "rate_limit",
    "too many requests",
    "overloaded",
    "service unavailable",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "econnreset",
    "try again",
)

# Status codes only with HTTP/provider framing, or a word-bounded code next to a reason phrase.
# Framers use (?<![A-Za-z]) so "AssertionError: 429" / "encode: 429" / "teststatus 503" do not
# match; a trailing \b on the code rejects digit-run FPs like "status 4294".
_TRANSIENT_STATUS_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z])https?(?:/\d+\.\d)?\s*"           # HTTP 429 / HTTP/1.1 503
    r"|(?<![A-Za-z])status(?:\s*code)?\s*[:=]?\s*"  # status 503 / status code: 502
    r"|(?<![A-Za-z])error(?:\s*code)?\s*[:=]?\s*"   # error 429 / error code: 429
    r"|(?<![A-Za-z])code\s*[:=]\s*"                 # code: 429 (colon/equals required)
    r")"
    r"(?:429|502|503|504)\b"
    r"|"
    r"\b(?:429|502|503|504)\b\s*[-:]?\s*"
    r"(?:too many|rate[_ ]?limit|bad gateway|service unavailable|gateway timeout|overloaded)",
    re.IGNORECASE,
)


def is_transient_failure(result: AgentResult) -> bool:
    """True if ``result`` is a FAILED run whose error looks like a transient infra/transport problem.

    Only ``RunStatus.FAILED`` qualifies: a ``TIMED_OUT`` retry would burn another full timeout
    window, and ``SUCCEEDED`` / ``EMPTY`` are not failures. An empty error string never matches - we
    do not retry a failure we cannot attribute to a transient cause.
    """
    if result.status is not RunStatus.FAILED:
        return False
    text = (result.error or "").lower()
    if not text:
        return False
    if any(marker in text for marker in TRANSIENT_MARKERS):
        return True
    return _TRANSIENT_STATUS_RE.search(text) is not None


class RetryPolicy(BaseModel):
    """How many times to re-run a transiently-failed run, and how long to wait between attempts."""

    max_attempts: int = 1          # 1 = no retry; N = up to N-1 retries on a transient failure
    backoff_base_s: float = 1.0    # first wait; grows by backoff_factor each subsequent attempt
    backoff_factor: float = 2.0

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait AFTER attempt number ``attempt`` (1-based) before the next one."""
        return self.backoff_base_s * (self.backoff_factor ** (attempt - 1))
