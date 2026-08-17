"""ZCode CLI adapter (Z.ai's GLM coding agent).

Invocation reference (zcode 0.16.3, verified against the shipped binary):

    zcode --cwd <worktree> --mode <plan|edit|yolo> --json --no-color
          [--resume <sess_...>] [--disallowed-tools <list>] --prompt <PROMPT>

**ZCode ships no PATH binary.** The headless CLI is a Node bundle inside the desktop app
(``ZCode.app/Contents/Resources/glm/zcode.cjs``), so ``binary`` is only the *optional* shim
name; ``resolve_launcher`` finds the real entry point and returns an argv prefix that may be
``["node", "<...>/zcode.cjs"]``. Resolution order, first hit wins:

  1. ``ZCODE_BIN`` in the client's ``env:`` block (fleet.config.yaml) - explicit, per client
  2. ``MARSHAL_ZCODE_BIN`` in the parent environment - explicit, machine-wide
  3. ``zcode`` on PATH - if the user made a shim
  4. the known app-bundle paths for this platform

`--json` emits a single JSON object on stdout::

    {"sessionId":"sess_...","traceId":"...","turnId":"...","response":"<final text>",
     "usage":{"totalTokens":N,"inputTokens":N,"outputTokens":N,"reasoningTokens":N,
              "cacheCreationTokens":N,"cacheReadTokens":N},
     "eventCount":N,
     "projection":{"status":"...","turnCount":N,"totalTokenCount":N,
                   "contextUsed":N|null,"contextWindow":N|null}}

``usage`` is emitted only when the turn actually reported it, and it carries **tokens only -
never a cost**. So usage is recorded with real token counts and ``source=unavailable``: Marshal
does not price a run it was not given a price for (OpenCode/Goose parity).

Permission modes. ZCode has five; only three are safe headless, because the shared runner closes
stdin and a prompting mode would hit EOF (or hang) instead of proceeding:

  * ``plan``  - "Inspect the code and present a plan before editing"   -> READ_ONLY
  * ``edit``  - "Edit automatically" (non-prompting)                   -> SAFE_EDIT
  * ``yolo``  - "Full access. Edit and run commands"                   -> YOLO
  * ``build`` - "Ask before each file change"      PROMPTING - never used
  * ``auto``  -                                    PROMPTING - never used

Note that ZCode defaults ``--prompt`` runs to ``yolo``; Marshal always passes ``--mode``
explicitly so the tier is never inherited from that default.

Model routing goes through the environment, not a flag: ZCode has no ``--model``, and the
``--settings`` flag its own ``--help`` advertises is **not implemented in 0.16.3**. ``prepare()``
stamps ``ZCODE_MODEL`` (which ZCode parses as ``model`` or ``provider/model``) so each run is
routed independently without writing shared config. ``--max-turns`` and ``--allowed-tools`` are
likewise advertised-but-rejected; do not add them without re-probing the installed build.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

from ..core.types import (
    AgentResult,
    Capabilities,
    ModelCatalog,
    ModelSource,
    PermissionFidelity,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from ..runtime.env import DETACHED_STDIO
from .base import CodingAgentBackend

_VERSION_PROBE_TIMEOUT_S = 20.0

#: Client ``env:`` key (fleet.config.yaml) and parent-env key holding an explicit launcher path.
_CLIENT_BIN_KEY = "ZCODE_BIN"
_ENV_BIN_KEY = "MARSHAL_ZCODE_BIN"

#: Where the headless Node bundle lives inside an installed ZCode desktop app, per platform.
#: ``~`` and ``$LOCALAPPDATA`` are expanded at resolve time.
_BUNDLE_CANDIDATES: tuple[str, ...] = (
    # macOS
    "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
    "~/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
    # Linux (AppImage extracted / package install)
    "/opt/ZCode/resources/glm/zcode.cjs",
    "~/.local/share/ZCode/resources/glm/zcode.cjs",
    # Windows
    "$LOCALAPPDATA/Programs/ZCode/resources/glm/zcode.cjs",
)

#: Suffixes that mean "this is a script, not an executable" and must be run through node.
_NODE_SUFFIXES = (".cjs", ".js", ".mjs")

#: Env var ZCode reads for `model` / `provider/model` routing (see prepare()).
_MODEL_ENV_KEY = "ZCODE_MODEL"

#: `projection.status` values that mean the turn did not finish cleanly.
_FAILED_STATUSES = frozenset({"error", "failed", "paused", "waiting"})

#: Static catalog — ZCode exposes no headless model-list probe. Bare ids work; the
#: ``provider/model`` form (e.g. ``builtin:zai-start-plan/glm-5.3``) pins the provider too.
_STATIC_MODELS: tuple[str, ...] = (
    "glm-5.3",
    "glm-5.2",
    "glm-5-turbo",
)


class ZCodeBackend(CodingAgentBackend):
    name = "zcode"
    #: Only the optional PATH shim; the real entry point comes from ``resolve_launcher``.
    binary = "zcode"
    credential_env_vars = ("ZCODE_API_KEY", "ANTHROPIC_API_KEY")
    capabilities = Capabilities(
        json_output=True,
        native_usage=False,  # tokens only, never a cost — see module docstring
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
        permission_fidelity=PermissionFidelity.BOUNDARY_ONLY,
    )

    _PERMISSION: ClassVar[dict[PermissionMode, list[str]]] = {
        PermissionMode.READ_ONLY: ["--mode", "plan"],
        PermissionMode.SAFE_EDIT: ["--mode", "edit"],
        PermissionMode.YOLO: ["--mode", "yolo"],
    }

    # --- launcher resolution -------------------------------------------------------------

    def resolve_launcher(self, client_env: dict[str, str] | None = None) -> list[str]:
        """Argv prefix that runs the headless CLI. Never raises; never returns empty.

        Falls back to ``[self.binary]`` when nothing resolves, so a missing install surfaces as
        the shared runner's actionable ``binary not found on PATH`` error rather than an
        exception escaping ``build_invocation``.
        """
        explicit = (client_env or {}).get(_CLIENT_BIN_KEY) or os.environ.get(_ENV_BIN_KEY)
        if explicit:
            return _as_launcher(Path(explicit).expanduser())
        shim = shutil.which(self.binary)
        if shim:
            return [shim]
        for candidate in _BUNDLE_CANDIDATES:
            path = Path(os.path.expandvars(candidate)).expanduser()
            if path.is_file():
                return _as_launcher(path)
        return [self.binary]

    # --- hooks ---------------------------------------------------------------------------

    def check_available(self) -> bool:
        """The no-client case of ``available_for_client`` - one implementation, not two."""
        return self.available_for_client(None)

    def available_for_client(self, client_env: dict[str, str] | None = None) -> bool:
        """Probe the resolved launcher, not ``shutil.which(binary)``.

        The base implementation assumes the backend is a PATH executable, which ZCode is not.

        ``client_env`` is threaded through so the probe resolves the SAME launcher
        ``build_invocation`` will use. Without it, a client whose ``env.ZCODE_BIN`` is the only
        thing naming the install (no PATH shim, no app bundle at a known path) was reported
        unavailable and skipped - while the invocation it was skipped in favour of would have
        launched perfectly.
        """
        launcher = self.resolve_launcher(client_env)
        if launcher == [self.binary] and shutil.which(self.binary) is None:
            return False
        try:
            proc = subprocess.run(
                [*launcher, "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_PROBE_TIMEOUT_S,
                check=False,
                **DETACHED_STDIO,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def unavailable_detail(self) -> str:
        return (
            "ZCode CLI not found: no ZCODE_BIN / MARSHAL_ZCODE_BIN, no 'zcode' on PATH, and no "
            "ZCode desktop app bundle at a known path (the headless CLI ships inside the app)"
        )

    def available_models(self) -> ModelCatalog:
        """Curated static ids — ZCode has no headless model-list command."""
        return ModelCatalog(models=list(_STATIC_MODELS), source=ModelSource.STATIC)

    def prepare(self, opts: RunOpts) -> None:
        """Route the model through ``ZCODE_MODEL`` — ZCode has no ``--model`` flag.

        ZCode parses the value as ``model`` or ``provider/model``. Passed through verbatim so a
        driver can pin the provider; Marshal never rewrites or guesses one.
        """
        if opts.model:
            opts.extra_env[_MODEL_ENV_KEY] = opts.model

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = self.resolve_launcher(opts.client_env)
        # --cwd is passed explicitly even though the runner sets the process cwd: ZCode discovers
        # project config (zcode.json / .zcode/config.json) relative to it, so the worktree must be
        # what it sees, not an inherited default.
        argv += ["--cwd", str(opts.cwd)]
        argv += self.map_permission(opts.permission)
        argv += ["--json", "--no-color"]
        if opts.session_id:
            argv += ["--resume", opts.session_id]
        argv += ["--prompt", self._compose_prompt(task)]
        return argv

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        obj = _parse_result(raw_stdout)
        if obj is None:
            # No parseable object (auth failure, provider error, crash, empty). base.run() fills
            # the reason from the exit code + stderr tail, so failure is never a silent success.
            return AgentResult(
                status=RunStatus.FAILED,
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )

        text = obj.get("response")
        text = text.strip() if isinstance(text, str) else ""
        session_id = obj.get("sessionId")
        session_id = session_id if isinstance(session_id, str) and session_id else None

        # ZCode reports turn health in `projection.status`; anything but a clean finish is a
        # failure even on exit 0, so a run that errored mid-turn is not integrated as a success.
        projection = obj.get("projection")
        projection = projection if isinstance(projection, dict) else {}
        status = projection.get("status")
        bad_status = isinstance(status, str) and status.lower() in _FAILED_STATUSES

        ok = exit_code == 0 and not bad_status
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if ok else RunStatus.FAILED,
            text=text,
            session_id=session_id,
            usage=_extract_usage(obj, self.name),
            error=None if ok else (f"zcode turn status {status!r}" if bad_status else text or None),
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


# --- module helpers ----------------------------------------------------------------------


def _as_launcher(path: Path) -> list[str]:
    """Argv prefix for a resolved entry point: node-prefixed for a script, bare otherwise."""
    if path.suffix.lower() in _NODE_SUFFIXES:
        return [shutil.which("node") or "node", str(path)]
    return [str(path)]


def _parse_result(raw: str) -> dict[str, Any] | None:
    """Parse the single JSON object `--json` emits. None if unparseable."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        # Defensive: salvage the object if anything preceded it on the stream.
        start = raw.find("{")
        if start <= 0:
            return None
        try:
            obj = json.loads(raw[start:])
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None


def _extract_usage(obj: dict[str, Any], backend_name: str) -> UsageRecord | None:
    """Token usage from the result object. Always `UNAVAILABLE` source — ZCode reports no cost.

    Falls back to `projection.totalTokenCount` when the `usage` block is absent, so a run whose
    tokens are only reported in the projection still lands honest counts in the ledger. Returns
    None when no token count was reported at all, rather than a fabricated zero-token record.
    """
    usage = obj.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = _as_int(usage.get("inputTokens"))
    output_tokens = _as_int(usage.get("outputTokens"))
    cache_read = _as_int(usage.get("cacheReadTokens"))

    if input_tokens + output_tokens <= 0:
        projection = obj.get("projection")
        projection = projection if isinstance(projection, dict) else {}
        total = _as_int(projection.get("totalTokenCount"))
        if total <= 0:
            return None
        # Only a total is known; attributing it to input or output would be a guess, so it is
        # recorded as input (the field the ledger sums) with output left at its honest zero.
        input_tokens = total

    return UsageRecord(
        backend=backend_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cost_usd=0.0,
        # ZCode emits tokens but never a price. Marshal does not price runs itself, so this
        # stays UNAVAILABLE — a $0 NATIVE here would assert a free run that no one reported.
        source=UsageSource.UNAVAILABLE,
    )


def _as_int(value: object) -> int:
    """Non-negative int from a loosely-typed JSON number. 0 for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))
