"""Stub harness: applies the golden patch. Zero cost, proves harness correctness."""

from __future__ import annotations

from .base import Harness, HarnessContext


class StubHarness(Harness):
    name = "stub"

    def __init__(self, patch_text: str = ""):
        # patch_text injected by `task run` from bundle.golden_patch_text().
        self.patch_text = patch_text

    def solve(self, container, ctx: HarnessContext) -> None:
        if not self.patch_text:
            raise ValueError("stub harness got no golden patch text")
        from ..container import apply_patch
        apply_patch(container, ctx.workdir, self.patch_text)
        self.transcript = "stub: applied the bundle's golden patch.diff directly, no LLM involved."
