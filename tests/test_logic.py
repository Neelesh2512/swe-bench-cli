"""Pure-logic tests (no Docker). Run: uv run pytest"""

from pathlib import Path

from swe_bench_cli import analyse as A
from swe_bench_cli import db as DB
from swe_bench_cli import runner as R
from swe_bench_cli.bundle import Bundle

BUNDLE = Path(__file__).parent.parent / "bundles" / "ansible-check-type-dict"


def test_expected_outcome():
    assert R.expected_outcome("pass2pass", "baseline") == "passed"
    assert R.expected_outcome("pass2pass", "post_solver") == "passed"
    assert R.expected_outcome("fail2pass", "baseline") == "failed"
    assert R.expected_outcome("fail2pass", "post_solver") == "passed"


def test_bundle_load_and_image_resolution():
    b = Bundle.load(BUNDLE)
    assert b.task_id == "ansible__ansible-d9f1866"
    assert b.fail2pass and b.pass2pass
    # base_image set -> image resolves to the locally built task image tag
    assert b.image == b.built_image_tag == "task-ansible__ansible-d9f1866:base"

    b.env.base_image = None
    # base_image absent -> falls back to prebuilt registry:dockerhub_tag
    assert b.image == f"jefzda/sweap-images:{b.dockerhub_tag}"


def test_harness_defaults_loaded_and_api_key_never_in_schema():
    b = Bundle.load(BUNDLE)
    assert b.harness_defaults.harness == "opencode"
    assert b.harness_defaults.model == "opencode/north-mini-code-free"
    assert b.harness_defaults.api_base is None
    # api_key must never be a settable field on HarnessDefaults -- task.json
    # is meant to be committed/shared, a key never belongs in it.
    assert not hasattr(b.harness_defaults, "api_key")


def test_db_roundtrip_and_expectation_flag():
    c = DB.connect(":memory:")
    cid = DB.start_command(c, "run", "t1", {"harness": "stub"}, "2026-01-01T00:00:00Z")
    DB.add_test_result(c, cid, "fail2pass", "f::t", "post_solver", "passed",
                       R.expected_outcome("fail2pass", "post_solver"))
    DB.add_test_result(c, cid, "fail2pass", "f::t", "baseline", "passed",
                       R.expected_outcome("fail2pass", "baseline"))  # unexpected pass
    DB.finish_command(c, cid, status="success", finished_at="2026-01-01T00:01:00Z")

    cmd, res = DB.get_command(c, cid)
    assert cmd["command"] == "run"
    flags = {r["phase"]: r["passed_expectation"] for r in res}
    assert flags["post_solver"] == 1   # passed == expected(passed)
    assert flags["baseline"] == 0      # passed != expected(failed)


def test_analyse_baseline_lookup_and_prompt_building():
    c = DB.connect(":memory:")
    vid = DB.start_command(c, "validate", "t1", {"bundle": "b"}, "2026-01-01T00:00:00Z")
    DB.add_test_result(c, vid, "fail2pass", "f::t", "baseline", "failed", "failed")
    DB.finish_command(c, vid, status="success", finished_at="2026-01-01T00:01:00Z",
                      summary="baseline: fail2pass 1/1 as expected")

    rid = DB.start_command(c, "run", "t1", {"harness": "stub"}, "2026-01-01T00:02:00Z")
    DB.add_test_result(c, rid, "fail2pass", "f::t", "post_solver", "passed", "passed")
    DB.finish_command(c, rid, status="success", harness="stub", solver_patch="diff --git a/x b/x",
                      transcript="stub: applied golden patch", finished_at="2026-01-01T00:03:00Z",
                      summary="post_solver: fail2pass 1/1 passed")

    cmd, after = DB.get_command(c, rid)
    baseline_cmd, baseline_results = A.get_baseline(c, "t1", rid)
    assert baseline_cmd["id"] == vid
    assert baseline_results[0]["outcome"] == "failed"

    prompt = A.build_prompt(cmd, after, baseline_cmd, baseline_results)
    assert "Before Run" in prompt or "Before run" in prompt
    assert "stub: applied golden patch" in prompt
    assert "diff --git a/x b/x" in prompt

    # no earlier validate for a task_id that never had one -> graceful, not a crash
    other_cmd, _ = DB.get_command(c, rid)
    none_cmd, none_results = A.get_baseline(c, "unknown-task", rid)
    assert none_cmd is None and none_results == []
    prompt2 = A.build_prompt(other_cmd, after, none_cmd, none_results)
    assert "no prior" in prompt2
