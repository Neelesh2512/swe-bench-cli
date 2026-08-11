"""Harness interface.

A harness (opencode, mini-swe-agent, stub) is the driver/framework that
receives the problem and mutates the repo *inside the container* in place.
The `solver` field on HarnessContext is the actual solver -- the LLM being
benchmarked -- since that's the thing whose capability this whole tool is
built to measure; the harness is just the scaffolding around it. The caller
captures `git diff` afterward to get the produced patch, so harnesses don't
return anything.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class HarnessContext:
    description: str          # problem statement (no gated test content)
    workdir: str             # repo path inside the container
    model: str | None = None  # the solver: an LLM id, e.g. "ollama/qwen2.5-coder:7b"
    api_base: str | None = None  # custom OpenAI-compatible endpoint, e.g. GLM/Kimi
    api_key: str | None = None   # paired with api_base; never persisted to disk/config


class Harness(ABC):
    name: str
    needs_network: bool = False
    """Set True if the harness process itself (not just our tool) makes
    outbound API calls from inside the container -- e.g. a single-binary
    agent like opencode that bundles the LLM call and the bash loop into one
    process. mini-swe-agent doesn't need this: its model call happens in our
    host Python process (litellm), only bash actions run in the container.
    """

    @abstractmethod
    def solve(self, container, ctx: HarnessContext) -> None:
        """Mutate the repo in `container` at ctx.workdir to fix the task.

        `container` is a docker SDK container handle; run commands via
        container.exec_run(...).
        """
        raise NotImplementedError
