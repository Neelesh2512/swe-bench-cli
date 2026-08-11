"""mini-swe-agent harness: agent loop (think->bash->observe) inside our container.

Default real harness. Wires mini-swe-agent's DefaultAgent + LitellmTextbasedModel
(regex-based action parsing, friendlier to local models than tool-calling) to
OUR docker container via a thin Environment adapter, so the container stays
one object across solve+grade (single lifecycle, one place to capture the
diff) instead of mini-swe-agent spinning up its own.

Solver-agnostic via litellm; default points at a local Ollama model — no paid
API dependency.
"""

from __future__ import annotations

from typing import Any

import yaml
from minisweagent.exceptions import Submitted

from .. import container as C
from ..exceptions import HarnessError
from .base import Harness, HarnessContext


class ContainerEnvironment:
    """Satisfies mini-swe-agent's Environment protocol using our own
    already-running docker container (see container.py), instead of letting
    mini-swe-agent manage container lifecycle itself.
    """

    def __init__(self, container, workdir: str, env: dict[str, str] | None = None):
        self.config = type("Cfg", (), {"env": env or {}})()
        self._container = container
        self._workdir = workdir

    def execute(self, action: dict, cwd: str = "") -> dict[str, Any]:
        command = action.get("command", "")
        rc, out = C.exec_run(self._container, command, workdir=cwd or self._workdir)
        result = {"output": out, "returncode": rc, "exception_info": ""}
        self._check_finished(result)
        return result

    def _check_finished(self, output: dict) -> None:
        """Raises Submitted if the output indicates task completion.

        Mirrors LocalEnvironment's own check (mini-swe-agent's prompt tells
        the model to `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` to finish).
        Without this, that command just runs as an ordinary no-op, the model
        never learns the loop is over, and it repeats "already done" turns
        until step_limit kills it -- observed and confirmed as the root
        cause of exactly that symptom before this fix.
        """
        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and output["returncode"] == 0:
            submission = "".join(lines[1:])
            raise Submitted({
                "role": "exit",
                "content": submission,
                "extra": {"exit_status": "Submitted", "submission": submission},
            })

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return {"system": "Linux", "release": "", "version": "", "machine": "", **kwargs}

    def serialize(self) -> dict:
        return {"info": {"config": {"environment_type": "ContainerEnvironment"}}}


class MiniSweAgentHarness(Harness):
    name = "mini-swe-agent"

    @staticmethod
    def _check_credentials(ctx: HarnessContext) -> None:
        """Fail fast with a clear message instead of a long retry loop.

        litellm classifies "missing API key" as InternalServerError for some
        providers rather than AuthenticationError (observed: openai) -- and
        mini-swe-agent's own retry wrapper only aborts immediately on
        AuthenticationError, so a missing key otherwise means 10 retries with
        exponential backoff (up to a minute apart) before the real error
        surfaces. api_key/api_base explicitly passed bypasses this check --
        litellm.validate_environment only knows about named env vars.
        """
        if ctx.api_key or ctx.api_base:
            return
        import litellm
        result = litellm.validate_environment(model=ctx.model)
        if not result.get("keys_in_environment", True):
            missing = ", ".join(result.get("missing_keys", [])) or "the provider's API key"
            raise HarnessError(
                f"missing credentials for solver '{ctx.model}': set {missing}, "
                f"or pass --api-key (with --api-base for a custom endpoint)."
            )

    def solve(self, container, ctx: HarnessContext) -> None:
        from minisweagent import package_dir
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel

        config = yaml.safe_load((package_dir / "config" / "default.yaml").read_text())

        agent_cfg = config["agent"] | {"step_limit": 30, "wall_time_limit_seconds": 900}
        model_cfg = config.get("model", {}) | {"cost_tracking": "ignore_errors"}
        if ctx.api_base:
            # Generic path for any OpenAI-compatible endpoint (GLM, Kimi/
            # Moonshot, a self-hosted vLLM server, etc.) that litellm doesn't
            # have a named provider for -- pass model="openai/<model-name>"
            # plus these, no litellm-side config needed.
            model_cfg = model_cfg | {"model_kwargs": {
                **model_cfg.get("model_kwargs", {}),
                "api_base": ctx.api_base,
                **({"api_key": ctx.api_key} if ctx.api_key else {}),
            }}
        self._check_credentials(ctx)

        model = LitellmTextbasedModel(model_name=ctx.model, **model_cfg)
        env = ContainerEnvironment(container, ctx.workdir, **config.get("environment", {}))
        agent = DefaultAgent(model, env, **agent_cfg)

        # agent.run() only raises on a genuine crash (its own exception
        # handler re-raises after recording the failure). A non-"Submitted"
        # exit_status (step/time limit, repeated format errors) just means
        # the agent gave up -- whatever it edited so far is still gradeable,
        # so we don't treat it as a harness failure here.
        try:
            self.last_result = agent.run(task=ctx.description)
        finally:
            # Full think->act->observe transcript: every LLM response
            # (THOUGHT + bash command) and every command's output, in order.
            # Captured even on a crash so a failed run isn't a total loss.
            self.transcript = "\n".join(
                f"=== {m.get('role', '?').upper()} ===\n{m.get('content', '')}"
                for m in agent.messages
            )
