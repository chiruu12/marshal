"""OpenCode adapter (`opencode run`).

Invocation reference (opencode CLI):

    opencode run --format json
                 [--agent plan | --dangerously-skip-permissions]
                 [-m provider/model] --dir CWD [-s SESSION] <PROMPT>

`--format json` emits an NDJSON event stream. Event shapes (from research):
  * text part      -> {"part":{"type":"text","text":"..."}}            (final message = concat)
  * step finish    -> {"part":{"type":"step-finish","reason":"stop",
                                "cost":<usd>,"tokens":{"input":..,"output":..,
                                                       "reasoning":..,"cache":{"read":..,"write":..}}}}
  * error          -> {"error":{"name":"...","data":{"message":"..."}}}

Known live-stream gaps (mitigated by the export-reconciliation step below):
  * The live stream can DROP the final `text` part - the agent's full final report is missing
    from stdout (observed with the GLM-5.2 / kimi models on long replies). The user has to
    re-run or finish the thread manually to recover it.
  * The live stream can also DROP the final `step-finish`, so cost/tokens drift to zero.
  * Mitigation: on a successful run we shell out to `opencode export <session_id>` once
    (cheap, ~100-500ms; reads the same on-disk session the CLI itself writes to) and use its
    authoritative `info.tokens`/`info.cost` and the full `messages[].parts[].text` to OVERRIDE
    whatever the live stream gave us. If the export fails (no binary, old CLI without
    `export`, corrupt session), the live stream stands - never crash a run over recovery.
  * Doctor auth: `opencode auth list` — any stored credential or active provider env var counts
    as authenticated (coarse multi-provider check; does not require the configured client's
    specific provider). Zero credentials + zero env auth → None / doctor FAIL via
    `verifies_auth`.

Other notes:
  * `opencode serve` (HTTP, 127.0.0.1:4096) is a faster warm-server path - added later
    (capabilities.server_mode = True).
  * serve+attach hangs if any permission is `ask`; for `run` headless we keep stdin closed
    (shared runner) so a stray prompt fails fast instead of deadlocking. ``prepare()`` stamps
    an engine-managed permission snippet via ``OPENCODE_CONFIG_CONTENT`` (``question: deny``
    always for write tiers; curated bash/edit/read denies for ``safe-edit``). Skip-permissions
    does NOT cover the ``question`` tool - the managed config closes that gap.
  * Canonical repo moved sst/opencode -> anomalyco/opencode; npm package still `opencode-ai`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from ..types import (
    AgentResult,
    Capabilities,
    PermissionFidelity,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from .base import CodingAgentBackend, parse_jsonl


#: How long to wait for `opencode export` to return the authoritative session JSON. The export
#: is a sqlite read of opencode's own on-disk session, so it's fast on a healthy install;
#: the bound here is for "the CLI is hung" / "the db is locked" - 15s is enough for a real
#: read and short enough that a stuck export can't delay a fleet run.
_EXPORT_TIMEOUT_S = 15.0

#: Env var OpenCode reads as an inline JSON config override (high precedence; no worktree dirty).
_OPENCODE_CONFIG_CONTENT = "OPENCODE_CONFIG_CONTENT"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CREDENTIALS_COUNT = re.compile(r"(\d+)\s+credentials\b", re.IGNORECASE)
_ENV_AUTH_COUNT = re.compile(r"(\d+)\s+environment\s+variables?\b", re.IGNORECASE)

#: Ordered bash deny patterns for ``safe-edit`` (inserted after ``"*": "allow"`` so last-match
#: wins). OpenCode's documented grammar is simple wildcards only (``*`` / ``?``); no regex,
#: brace expansion, or negative matching. These cover curated cheap cases (``rm``, ``git config``,
#: redirection / ``tee`` / ``sed`` into ``.env`` / ``.git``) - not complete shell mediation.
#: Wrappers (``sh -c``), env prefixes, alternate writers, and parser/version gaps can bypass.
SAFE_EDIT_BASH_DENIES: tuple[tuple[str, str], ...] = (
    ("rm", "deny"),
    ("rm *", "deny"),
    ("git config", "deny"),
    ("git config *", "deny"),
    ("*>*.env", "deny"),
    ("*>*.env.*", "deny"),
    ("*>*.git/*", "deny"),
    ("tee *.env", "deny"),
    ("tee *.env.*", "deny"),
    ("tee *.git/*", "deny"),
    ("sed *.env", "deny"),
    ("sed *.env.*", "deny"),
    ("sed *.git/*", "deny"),
)


def permission_config_for(mode: PermissionMode) -> dict[str, Any] | None:
    """Pure builder: permission snippet stamped for a write-tier run, or None for read-only.

    ``safe-edit`` gets curated denies (``question``, ``external_directory``, bash cheap cases,
    ``.env`` / ``.git`` edit+read). ``yolo`` still denies ``question`` so headless runs cannot
    deadlock on a prompt, but otherwise stays unrestricted. Never emits ``ask`` (stdin is closed).
    """
    if mode is PermissionMode.READ_ONLY:
        return None
    if mode is PermissionMode.YOLO:
        return {
            "$schema": "https://opencode.ai/config.json",
            "permission": {
                "*": "allow",
                "question": "deny",
            },
        }
    # SAFE_EDIT — catch-all allow first; specific denies later (last matching rule wins).
    bash: dict[str, str] = {"*": "allow"}
    bash.update(SAFE_EDIT_BASH_DENIES)
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "*": "allow",
            "question": "deny",
            "external_directory": "deny",
            "bash": bash,
            "edit": {
                "*": "allow",
                "*.env": "deny",
                "*.env.*": "deny",
                ".git/*": "deny",
                ".git/**": "deny",
            },
            "read": {
                "*": "allow",
                "*.env": "deny",
                "*.env.*": "deny",
                "*.env.example": "allow",
            },
        },
    }


class OpenCodeBackend(CodingAgentBackend):
    name = "opencode"
    binary = "opencode"
    capabilities = Capabilities(
        json_output=True,
        stream_json=True,
        sessions=True,
        server_mode=True,  # `opencode serve` - warm-server fast path wired in a later phase
        native_usage=True,
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
        permission_fidelity=PermissionFidelity.ENFORCED_DENIES,
    )

    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: ["--agent", "plan"],
        PermissionMode.SAFE_EDIT: ["--dangerously-skip-permissions"],
        PermissionMode.YOLO: ["--dangerously-skip-permissions"],
    }

    # --- hooks ---------------------------------------------------------------------------

    def account_info(self) -> dict[str, str] | None:
        """Auth via ``opencode auth list`` — any credential or provider env counts as authed.

        Coarse multi-provider check (YAGNI: does not match a client's specific provider).
        Zero credentials and zero env-auth lines, non-zero exit, timeout, or unparseable
        output → None. Never raises.
        """
        if shutil.which(self.binary) is None:
            return None
        try:
            proc = subprocess.run(
                [self.binary, "auth", "list"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        blob = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        return _parse_auth_list(blob)

    def verifies_auth(self) -> bool:
        # ``opencode auth list`` with ≥1 credential/env auth is the doctor auth signal.
        return True

    def prepare(self, opts: RunOpts) -> None:
        """Stamp ``OPENCODE_CONFIG_CONTENT`` with the permission snippet for this tier.

        Argv still uses ``--dangerously-skip-permissions`` for write tiers; the managed config
        is what makes ``safe-edit`` distinct (curated denies + ``question: deny``).
        """
        cfg = permission_config_for(opts.permission)
        if cfg is None:
            return
        opts.extra_env = {
            **opts.extra_env,
            _OPENCODE_CONFIG_CONTENT: json.dumps(cfg, separators=(",", ":")),
        }

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary, "run", "--format", "json"]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["-m", opts.model]
        argv += ["--dir", str(opts.cwd)]
        if opts.session_id:
            argv += ["-s", opts.session_id]
        argv.append(self._compose_prompt(task))
        return argv

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return self._parse_event_stream(raw_stdout, raw_stderr, exit_code)

    def finalize(self, result: AgentResult) -> AgentResult:
        if result.session_id and result.status is RunStatus.SUCCEEDED:
            reconciled = self._reconcile_from_export(result.session_id)
            if reconciled is not None:
                if reconciled.get("text") is not None:
                    result.text = reconciled["text"]
                if reconciled.get("usage") is not None:
                    result.usage = reconciled["usage"]
        return result

    def _parse_event_stream(
        self, raw_stdout: str, raw_stderr: str, exit_code: int
    ) -> AgentResult:
        events = parse_jsonl(raw_stdout)

        text_parts: list[str] = []
        session_id: str | None = None
        error_msg: str | None = None
        usage = UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE)
        found_usage = False
        found_cost = False

        for ev in events:
            err = ev.get("error")
            if isinstance(err, dict):
                data = err.get("data")
                if isinstance(data, dict) and data.get("message"):
                    error_msg = str(data["message"])
                else:
                    error_msg = error_msg or (str(err["name"]) if err.get("name") else None)

            sid = ev.get("sessionID") or ev.get("session_id")
            if isinstance(sid, str):
                session_id = sid

            part = ev.get("part")
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                txt = part.get("text")
                if isinstance(txt, str):
                    text_parts.append(txt)
            elif ptype in ("step-finish", "step_finish"):
                tokens_raw = part.get("tokens")
                tokens: dict[str, Any] = tokens_raw if isinstance(tokens_raw, dict) else {}
                cache_raw = tokens.get("cache")
                cache: dict[str, Any] = cache_raw if isinstance(cache_raw, dict) else {}
                usage.input_tokens += int(tokens.get("input", 0) or 0)
                usage.output_tokens += int(tokens.get("output", 0) or 0)
                usage.cache_read_tokens += int(cache.get("read", 0) or 0)
                cost = part.get("cost")
                if cost is not None:
                    usage.cost_usd += float(cost or 0)
                    found_cost = True
                found_usage = True

        # NATIVE only when the backend reported a POSITIVE cost. A reported $0 alongside consumed
        # tokens means the model is unpriced (e.g. a custom OpenAI-compatible provider opencode has no
        # price table for, like EastRouter) - NOT a free run - so it stays UNAVAILABLE rather than
        # claiming a fake $0. Tokens without any cost field also stay UNAVAILABLE (priced from the table).
        if found_cost and usage.cost_usd > 0:
            usage.source = UsageSource.NATIVE

        ok = exit_code == 0 and error_msg is None
        return AgentResult(
            status=RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
            text="".join(text_parts).strip(),
            session_id=session_id,
            usage=usage if found_usage else None,
            error=error_msg if not ok else None,
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )

    def _reconcile_from_export(self, session_id: str) -> dict[str, Any] | None:
        """Call `opencode export <session_id>` to recover the authoritative final text + usage.

        Returns ``{"text": str|None, "usage": UsageRecord|None}`` on success, or ``None`` when
        no recovery is possible (binary absent, subprocess error, unparseable JSON, no
        messages). Never raises: a completed run must not be invalidated by a recovery
        attempt. The live event stream stands as the fallback.

        The export command writes its status line ("Exporting session: <id>") to STDERR; the
        JSON payload is on STDOUT. We parse from the first ``{`` defensively in case a future
        opencode version starts writing a leading log line to stdout.
        """
        if shutil.which(self.binary) is None:
            return None
        try:
            proc = subprocess.run(
                [self.binary, "export", session_id],
                capture_output=True,
                text=True,
                timeout=_EXPORT_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        data = self._parse_export_payload(proc.stdout)
        if not isinstance(data, dict):
            return None
        return self._export_to_patch(data)

    @staticmethod
    def _parse_export_payload(raw: str) -> dict[str, Any] | None:
        """Parse the JSON object on stdout of `opencode export`. Returns None on any failure.

        The leading non-JSON line is stripped by finding the first ``{``; this is defensive
        against future upstream changes that may prepend a log line to stdout.
        """
        idx = raw.find("{")
        if idx < 0:
            return None
        try:
            obj: Any = json.loads(raw[idx:])
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _export_to_patch(data: dict[str, Any]) -> dict[str, Any] | None:
        """Map an export payload to ``{"text": ..., "usage": ...}`` (either may be None).

        The export is the authoritative ledger for the session: ``info.tokens`` and
        ``info.cost`` are the totals opencode itself recorded, and ``messages[].parts[].text``
        concatenates to the full final report (live stream drops parts on long replies).
        Returns None only when the export yielded nothing actionable (no text, no tokens).
        """
        # Final text: every text part in order. Concatenating all text parts (across messages
        # and within each message) yields the same string the live event stream would have
        # produced IF it had not dropped parts - which is exactly the gap we are closing.
        text_parts: list[str] = []
        for msg in data.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            for part in msg.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    txt = part.get("text")
                    if isinstance(txt, str):
                        text_parts.append(txt)
        final_text = "".join(text_parts).strip() or None

        # Authoritative usage: prefer info.tokens + info.cost (the export's ledger).
        info = data.get("info")
        usage: UsageRecord | None = None
        if isinstance(info, dict):
            tokens_raw = info.get("tokens")
            cost_raw = info.get("cost")
            model_raw = info.get("model")
            if isinstance(tokens_raw, dict):
                cache_raw = tokens_raw.get("cache")
                cache: dict[str, Any] = cache_raw if isinstance(cache_raw, dict) else {}
                cost = float(cost_raw) if isinstance(cost_raw, (int, float)) else 0.0
                model_id = (
                    model_raw.get("id") if isinstance(model_raw, dict) else None
                )
                # NATIVE only when the export reports a positive cost - same rule as the live
                # event-stream path: a $0 cost alongside consumed tokens means "unpriced model",
                # not "free", so the cost stays UNAVAILABLE rather than fabricating zero.
                usage = UsageRecord(
                    backend="opencode",
                    source=UsageSource.NATIVE if cost > 0 else UsageSource.UNAVAILABLE,
                    model=model_id if isinstance(model_id, str) else None,
                    input_tokens=int(tokens_raw.get("input", 0) or 0),
                    output_tokens=int(tokens_raw.get("output", 0) or 0),
                    cache_read_tokens=int(cache.get("read", 0) or 0) if isinstance(cache, dict) else 0,
                    cost_usd=cost,
                )

        if final_text is None and usage is None:
            return None
        return {"text": final_text, "usage": usage}


def _parse_auth_list(raw: str) -> dict[str, str] | None:
    """Parse ``opencode auth list`` text. Authed when ≥1 credential or ≥1 env-auth line.

    Strips ANSI. Looks for summary lines like ``4 credentials`` / ``2 environment variables``.
    Zero of both → None. Pure.
    """
    text = _ANSI.sub("", raw or "")
    cred_m = _CREDENTIALS_COUNT.search(text)
    env_m = _ENV_AUTH_COUNT.search(text)
    n_cred = int(cred_m.group(1)) if cred_m else 0
    n_env = int(env_m.group(1)) if env_m else 0
    if n_cred < 1 and n_env < 1:
        return None
    return {"plan": "credentials" if n_cred >= 1 else "env"}
