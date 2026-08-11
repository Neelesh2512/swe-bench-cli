"""Run gated tests in a container and classify outcomes."""

from __future__ import annotations

from . import container as C


def run_test(cont, workdir: str, test_cmd: str, test_id: str) -> str:
    """Run one test node id; return 'passed' | 'failed' | 'error'.

    Relies only on the shell exit code, so it is test-framework agnostic:
    a bundle's test_cmd must exit non-zero when the selected test fails.
    """
    cmd = test_cmd.format(test_id=test_id)
    rc, out = C.exec_run(cont, cmd, workdir=workdir)
    if rc == 0:
        return "passed"
    # ponytail: exit-code only. If a runner conflates "collection error" with
    # "test failed", parse `out` per-framework here — not needed for pytest.
    return "failed"


def expected_outcome(bucket: str, phase: str) -> str:
    """What a test in this bucket should do at this phase.

    baseline:      p2p -> passed, f2p -> failed
    post_solver:   p2p -> passed, f2p -> passed
    """
    if bucket == "pass2pass":
        return "passed"
    # fail2pass
    return "failed" if phase == "baseline" else "passed"
