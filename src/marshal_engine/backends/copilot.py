"""GitHub Copilot CLI adapter (`copilot -p`).

Invocation reference (GitHub Copilot CLI 1.0.80, verified against the shipped binary):

    copilot -C <worktree> --output-format json --no-color --no-ask-user
            --disable-builtin-mcps --no-remote --no-auto-update
            <permission flags> [--model MODEL] [--session-id ID] -p <PROMPT>

`--output-format json` emits **JSONL** on stdout - one object per line, ending in a single
terminal `result` event::

    {"type":"assistant.message","data":{"content":"<text>","model":"gpt-5-mini",
                                        "outputTokens":17,...}}
    {"type":"tool.execution_complete","data":{"success":false,
                                              "error":{"message":"...","code":"denied"}}}
    {"type":"result","sessionId":"<uuid>","exitCode":0,
     "usage":{"premiumRequests":0,"totalApiDurationMs":9016,
              "codeChanges":{"linesAdded":1,"linesRemoved":0,"filesModified":[...]}}}

A hard startup failure (bad `--model`, missing auth) emits **no** `result` event at all - just
`Error: ...` on the stream - so an absent terminal event is treated as failure, never as a
silent success.

Usage is **output tokens only**. Copilot reports `outputTokens` per assistant message and
`premiumRequests` (a quota unit, not money) on the result; it never reports a USD cost or an
input-token count. So the ledger gets honest output tokens with `source=unavailable` - Marshal
does not price a run it was not given a price for (OpenCode/Goose/ZCode parity).

Permission tiers, all three verified live against the CLI:

  * ``--mode plan``        -> READ_ONLY. Genuinely enforced: a `create` call in plan mode comes
    back ``{"success":false,"error":{"code":"denied"}}`` and `filesModified` stays empty, even
    when ``--allow-all-tools`` is also passed.
  * ``--allow-all-tools`` + the curated ``SAFE_EDIT_DENY`` overlay -> SAFE_EDIT. Copilot's own
    docs state denial rules take precedence over allow rules "even --allow-all-tools", and a
    live probe confirms it: ``--deny-tool 'write(.env)'`` refused the write with
    ``Permission to run this tool was denied due to the following rules: `write(.env)` ``.
  * ``--allow-all``       -> YOLO. Deliberately unrestricted (tools + paths + urls).

``--allow-all-tools`` is required for any non-interactive run; without it the CLI wants approval
and the shared runner has closed stdin. ``--no-ask-user`` disables the `ask_user` tool for the
same reason.

Three flags are passed on every run to keep a fleet worker inside Marshal's isolation boundary:

  * ``--disable-builtin-mcps`` - Copilot otherwise auto-connects ``github-mcp-server`` with the
    user's GitHub token, which can open PRs and issues. That acts *outside* the worktree, so it
    is off by default; a driver that wants it configures an MCP server explicitly.
  * ``--no-remote`` - stops the unattended session being remote-controlled (and exported) from
    GitHub web/mobile mid-run.
  * ``--no-auto-update`` - a background CLI upgrade mid-fleet is nondeterminism, not a feature.

Model pinning is plan-gated. ``copilot help config`` lists the *binary's* catalog, not the
account's entitlements: on a **Copilot Free** plan every pinned id is rejected at startup with
``Error: Model "<id>" from --model flag is not available``, and only ``auto`` is accepted (it
routes to models that cost ``premiumRequests: 0``). Marshal passes ``--model`` through verbatim
and lets that rejection surface as an honest failure rather than silently rewriting the request.

Auth is a GitHub token, checked in this precedence order by the CLI itself:
``COPILOT_GITHUB_TOKEN``, ``GH_TOKEN``, ``GITHUB_TOKEN`` - or a stored OAuth credential from
``copilot login``. There is no cheap authenticated status subcommand, so ``verifies_auth()``
stays False: doctor reports CLI presence without claiming the credentials are valid.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from ..core.types import (
    AgentResult,
    Capabilities,
    ModelCatalog,
    PermissionFidelity,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from .base import CodingAgentBackend, parse_jsonl

#: Curated deny overlay for ``safe-edit`` (deny beats allow, including ``--allow-all-tools``).
#: Destructive shell, secrets, and the two ways a worktree run could reach past its own
#: boundary - pushing to the remote, or driving the GitHub API through ``gh``.
#:
#: These are curated rules, not a sandbox: ``write(<relative>)`` matches by trailing path
#: component, so it covers a name in any directory but does not glob suffixes - ``.env.local``
#: needs its own entry, which is why the common ones are listed explicitly. The worktree
#: remains the isolation boundary for everything not named here.
SAFE_EDIT_DENY: tuple[str, ...] = (
    "shell(rm)",
    "shell(git push)",
    "shell(gh:*)",
    "write(.env)",
    "write(.env.local)",
    "write(.env.production)",
    "write(.git)",
)

#: Always a valid ``--model`` value, and the only one the Copilot **Free** plan accepts: the
#: paid catalog ids below are rejected at startup with "Model ... is not available" on a free
#: account. ``copilot help config`` does not list it (it is documented on the flag itself), so
#: it is prepended to the probed catalog rather than parsed out of it.
_AUTO_MODEL = "auto"

#: Fallback when the installed CLI's model catalog cannot be read (see ``available_models``).
#: Sourced from docs/model-playbook.md (copilot rows).
_STATIC_MODELS: tuple[str, ...] = (
    _AUTO_MODEL,
    "claude-sonnet-5",
    "claude-haiku-4.5",
    "gpt-5.4-mini",
    "gpt-5-mini",
)

#: Matches one bullet of the model catalog in ``copilot help config`` - ``    - "gpt-5-mini"``.
_MODEL_LINE = re.compile(r'^\s*-\s*"([A-Za-z0-9][\w.\-]*)"\s*$')


class CopilotBackend(CodingAgentBackend):
    name = "copilot"
    binary = "copilot"
    static_models: ClassVar[tuple[str, ...]] = _STATIC_MODELS
    verified_version: ClassVar[str | None] = "GitHub Copilot CLI 1.0.80."
    # Precedence order is the CLI's own (see `copilot help environment`), preserved here so the
    # child sees the same one a human shell would.
    credential_env_vars = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
    capabilities = Capabilities(
        json_output=True,
        native_usage=False,  # output tokens only, never a cost — see module docstring
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
        permission_fidelity=PermissionFidelity.ENFORCED_DENIES,
    )

    # Marshal's three tiers -> Copilot's native flags. Every tier is non-prompting: the shared
    # runner closes stdin, so a mode that waits for approval would hit EOF instead of proceeding.
    _PERMISSION: ClassVar[dict[PermissionMode, list[str]]] = {
        # plan mode is the enforcement; --allow-all-tools only stops *read* tools prompting.
        PermissionMode.READ_ONLY: ["--mode", "plan", "--allow-all-tools"],
        PermissionMode.SAFE_EDIT: [
            "--allow-all-tools",
            *[flag for rule in SAFE_EDIT_DENY for flag in ("--deny-tool", rule)],
        ],
        # Intentionally unrestricted: all tools, all paths, all urls, no deny overlay.
        PermissionMode.YOLO: ["--allow-all"],
    }

    # --- hooks ---------------------------------------------------------------------------

    def available_models(self) -> ModelCatalog:
        """Model ids the *installed CLI* accepts, read from ``copilot help config``.

        `PROBED` because the CLI answered just now, so the list tracks CLI upgrades instead of
        rotting in this file. It is still the binary's catalog, **not an entitlement check** -
        verified against a Copilot Free account, which rejects every pinned id here ("Model
        ... is not available") and accepts only ``auto``. Degrades to the curated `STATIC`
        list on any failure.
        """
        return self._probe_models(
            [self.binary, "help", "config"],
            _parse_model_catalog,
            self.static_models,
        )

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary, "-C", str(opts.cwd), "--output-format", "json", "--no-color"]
        # Headless hygiene + isolation, on every tier — see the module docstring for why each.
        argv += ["--no-ask-user", "--disable-builtin-mcps", "--no-remote", "--no-auto-update"]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["--model", opts.model]
        if opts.session_id:
            # --session-id takes a required value; --resume's is optional, so `--resume <id>`
            # would parse the id as a positional prompt instead of the session to resume.
            argv += ["--session-id", opts.session_id]
        argv += ["-p", self._compose_prompt(task)]
        return argv

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        events = parse_jsonl(raw_stdout)

        result_ev: dict[str, Any] | None = None
        text_parts: list[str] = []
        error_msg: str | None = None
        output_tokens = 0

        # `tool.execution_complete` events carry `{"success":false,"code":"denied"}` when the
        # deny overlay refuses a call. That is the overlay working, not a failed run — the agent
        # is expected to route around it — so those are deliberately not collected as errors.
        for ev in events:
            etype = ev.get("type", "")
            data = ev.get("data")
            data = data if isinstance(data, dict) else {}

            if etype == "result":
                result_ev = ev
            elif etype == "assistant.message":
                # The streamed `assistant.message_delta` events repeat this content chunk by
                # chunk; only the consolidated message is collected so text is not duplicated.
                content = data.get("content")
                if isinstance(content, str) and content.strip():
                    text_parts.append(content.strip())
                output_tokens += _as_int(data.get("outputTokens"))
            elif etype in ("error", "session.error"):
                msg = data.get("message") or ev.get("message")
                if isinstance(msg, str) and msg.strip():
                    error_msg = error_msg or msg.strip()

        if result_ev is None:
            # No terminal event: a startup failure (bad model, missing auth) or a kill. base.run()
            # fills the reason from the exit code + stderr tail, so this is never a silent success.
            return AgentResult(
                status=RunStatus.FAILED,
                text="\n".join(text_parts).strip(),
                error=error_msg,
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )

        sid = result_ev.get("sessionId")
        session_id = sid if isinstance(sid, str) and sid else None

        # Copilot's own exit code rides on the result event. Trust the stricter of the two: a
        # process that exited 0 while reporting a non-zero result did not succeed.
        reported = result_ev.get("exitCode")
        reported_ok = reported == 0 if isinstance(reported, int) else False
        ok = exit_code == 0 and reported_ok and error_msg is None

        return AgentResult(
            status=RunStatus.EXITED_CLEAN if ok else RunStatus.FAILED,
            text="\n".join(text_parts).strip(),
            session_id=session_id,
            usage=_usage(result_ev, output_tokens, self.name),
            error=(error_msg or f"copilot reported exitCode {reported!r}") if not ok else None,
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


# --- module helpers ----------------------------------------------------------------------


def _parse_model_catalog(raw: str) -> list[str]:
    """Extract quoted model ids from the ``model:`` section of ``copilot help config``.

    Scoped to that one section: the help text lists quoted enum values for several other keys
    (log levels, context tiers), and scanning the whole document would mix them into the
    catalog. `auto` leads the result because it is a valid `--model` value that this section
    never lists - and the only one a free account can actually use. Returns [] if the section
    is absent, which the shared probe treats as a failure and answers from the curated list.
    """
    models: list[str] = []
    in_section = False
    for line in raw.splitlines():
        if not in_section:
            if "`model`:" in line:
                in_section = True
            continue
        match = _MODEL_LINE.match(line)
        if match:
            models.append(match.group(1))
            continue
        # The section ends at the first line that is neither a model bullet nor blank.
        if line.strip():
            break
    return [_AUTO_MODEL, *models] if models else []


def _usage(result_ev: dict[str, Any], output_tokens: int, backend_name: str) -> UsageRecord | None:
    """Usage from the terminal result. Always `UNAVAILABLE` — Copilot reports no cost.

    `premiumRequests` counts quota units, not money, and `totalNanoAiu` is an internal credit
    unit; neither is a USD figure, so pricing either would be a fabrication. Input tokens are
    never reported at all and stay at their honest zero. None when nothing was reported.
    """
    usage = result_ev.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    changes = usage.get("codeChanges")
    changes = changes if isinstance(changes, dict) else {}
    touched = bool(changes.get("filesModified")) or output_tokens > 0
    if not touched:
        return None
    return UsageRecord(
        backend=backend_name,
        output_tokens=output_tokens,
        cost_usd=0.0,
        source=UsageSource.UNAVAILABLE,
    )


def _as_int(value: object) -> int:
    """Non-negative int from a loosely-typed JSON number. 0 for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))
