"""Harness registry."""

from .base import Harness
from .stub import StubHarness

# mini-swe-agent / opencode registered lazily to avoid importing their deps
# unless actually selected.
REGISTRY: dict[str, type[Harness]] = {
    "stub": StubHarness,
}


def get_harness(name: str) -> Harness:
    if name == "mini-swe-agent":
        from .mini_swe_agent import MiniSweAgentHarness
        return MiniSweAgentHarness()
    if name == "opencode":
        from .opencode import OpenCodeHarness
        return OpenCodeHarness()
    if name not in REGISTRY:
        raise ValueError(f"unknown harness '{name}'; choices: stub, mini-swe-agent, opencode")
    return REGISTRY[name]()
