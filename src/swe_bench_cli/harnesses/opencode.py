"""opencode harness: external Go agent, run headless via subprocess in-container.

Unlike mini-swe-agent (model call happens in our host process, only bash
actions are containerized), opencode is a single binary that bundles the LLM
call *and* the bash loop into one process. Since we run it inside the
container, the container needs real outbound network for opencode to reach
its solver's gateway -- see Harness.needs_network. Uses opencode's own free
`north-mini-code-free` model by default: no API key, no paid tool.
"""

from __future__ import annotations

import json
import os
import re

from .. import container as C
from ..exceptions import HarnessError
from .base import Harness, HarnessContext

_PROVIDER_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "OPENROUTER_API_KEY",
]
"""Forwarded from the host into the exec call (not the container's own
Config.Env -- see container.exec_run) if set, so `--solver <provider>/...`
works for any provider opencode supports. Never baked into the image or a
run-snapshot; scoped to this one exec call only."""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_CUSTOM_KEY_ENV_VAR = "CUSTOM_API_KEY"
"""Generic fallback for a --api-base provider opencode has no built-in entry
for (GLM, Kimi/Moonshot, a self-hosted endpoint, ...). The written
opencode.json only ever contains the *name* of this var, never the key
itself -- the key is forwarded at exec time same as the named ones above."""

_INSTALL_CHECK = "command -v opencode || ls ~/.opencode/bin/opencode 2>/dev/null"
_INSTALL_CMD = "curl -fsSL https://opencode.ai/install | bash"
_OPENCODE_BIN = "~/.opencode/bin/opencode"
_OPENCODE_LOG = "$HOME/.local/share/opencode/log/opencode.log"

_IDLE_LIMIT_SEC = 90
"""Every productive opencode step appends to its own log within seconds
(observed gaps: 2-20s across all runs). A stuck gateway call produces zero
new log lines. Killing on *inactivity* -- not on total elapsed time -- means
a bundle that legitimately needs longer isn't punished, while a genuine hang
gets caught quickly regardless of how long the run has been going."""

_MAX_WALL_SEC = 3600
"""Defense in depth only: catches a pathological case where the log itself
keeps ticking (e.g. a runaway retry loop) without ever finishing."""

_WATCHDOG_SCRIPT = f"""#!/bin/sh
set -u
LOG="{_OPENCODE_LOG}"
START=$(date +%s)
"$@" &
PID=$!
while kill -0 "$PID" 2>/dev/null; do
    sleep 5
    NOW=$(date +%s)
    if [ -f "$LOG" ]; then
        LASTMOD=$(stat -c %Y "$LOG")
    else
        LASTMOD=$START
    fi
    IDLE=$((NOW - LASTMOD))
    ELAPSED=$((NOW - START))
    if [ "$IDLE" -gt {_IDLE_LIMIT_SEC} ]; then
        kill -9 "$PID" 2>/dev/null
        echo "__IDLE_KILL__ elapsed=${{ELAPSED}}s idle=${{IDLE}}s"
        break
    fi
    if [ "$ELAPSED" -gt {_MAX_WALL_SEC} ]; then
        kill -9 "$PID" 2>/dev/null
        echo "__MAXWALL_KILL__ elapsed=${{ELAPSED}}s"
        break
    fi
done
wait "$PID" 2>/dev/null
"""


class OpenCodeHarness(Harness):
    name = "opencode"
    needs_network = True

    def solve(self, container, ctx: HarnessContext) -> None:
        rc, _ = C.exec_run(container, _INSTALL_CHECK)
        if rc != 0:
            rc, out = C.exec_run(container, _INSTALL_CMD)
            if rc != 0:
                raise HarnessError(f"opencode install failed (rc={rc}):\n{out}")

        C.put_file(container, f"{ctx.workdir}/.__task_description.md", ctx.description)
        C.put_file(container, f"{ctx.workdir}/.__run_opencode.sh", _WATCHDOG_SCRIPT)

        model = ctx.model or "opencode/north-mini-code-free"
        forwarded_env = {k: os.environ[k] for k in _PROVIDER_ENV_VARS if k in os.environ}

        if ctx.api_base:
            # Generic OpenAI-compatible provider (GLM, Kimi/Moonshot, a
            # self-hosted vLLM/ollama-compatible server, ...) opencode has no
            # built-in entry for. --solver must be "<provider-id>/<model-name>"
            # -- provider-id is arbitrary, just needs to match what we
            # register here. Config file only names the env var; the actual
            # key is forwarded at exec time, never written to disk.
            provider_id, _, model_name = model.partition("/")
            opencode_config = {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    provider_id: {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": ctx.api_base,
                            "apiKey": f"{{env:{_CUSTOM_KEY_ENV_VAR}}}",
                        },
                        "models": {model_name: {}},
                    }
                },
            }
            C.put_file(container, f"{ctx.workdir}/opencode.json", json.dumps(opencode_config))
            if ctx.api_key:
                forwarded_env[_CUSTOM_KEY_ENV_VAR] = ctx.api_key

        cmd = (
            f'chmod +x .__run_opencode.sh && ./.__run_opencode.sh '
            f'{_OPENCODE_BIN} run --auto -m {model} '
            f'"Fix the issue described in .__task_description.md, then delete that file."'
        )
        # tty=True: opencode's UI renderer can block on terminal-dependent
        # I/O when run fully headless. Costs nothing to keep even though it
        # didn't turn out to be the root cause of the hangs (see watchdog
        # above for the actual fix).
        rc, out = C.exec_run(container, cmd, workdir=ctx.workdir, tty=True, env=forwarded_env)
        # opencode's own terminal narration (which files it read/edited, its
        # closing summary) -- this IS what the model actually did, not a
        # separate log we have to go dig up.
        self.transcript = _ANSI_RE.sub("", out)
        if "__IDLE_KILL__" in out or "__MAXWALL_KILL__" in out:
            print(f"opencode: watchdog killed a stalled run; grading whatever it edited so far\n{out[-500:]}")
        elif rc != 0:
            print(f"opencode: exited rc={rc}; grading whatever it edited so far\n{out[-2000:]}")

        # Our own scaffolding must never show up in the captured diff --
        # capture_diff() runs `git add -A` right after solve() returns.
        # (.__task_description.md is usually deleted by the model itself per
        # the prompt, but rm -f here covers the case where it wasn't.)
        C.exec_run(container, "rm -f .__run_opencode.sh .__task_description.md opencode.json",
                   workdir=ctx.workdir)
