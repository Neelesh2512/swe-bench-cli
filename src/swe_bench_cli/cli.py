"""Typer CLI: init / validate / run / logs / analyse / bundles."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import typer
from rich.markdown import Markdown

from . import analyse as A
from . import bundle as B
from . import container as C
from . import db as DB
from . import display as UI
from . import runner as R
from .bundle import Bundle
from .harnesses import get_harness
from .harnesses.base import HarnessContext

app = typer.Typer(add_completion=False, help="Run SWE-bench-style coding evals in containers.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_image(cli, bundle: Bundle) -> str:
    """Build (base_image set) or pull (prebuilt dockerhub_tag) the task image.

    Cached: if a build already produced bundle.built_image_tag, reuse it
    instead of rebuilding on every command.
    """
    if bundle.env.base_image:
        try:
            return cli.images.get(bundle.built_image_tag).id
        except Exception:
            build_cmd = bundle.env.build_cmd.format(
                repo=bundle.repo, base_commit=bundle.base_commit, workdir=bundle.env.workdir,
            )
            return C.build_image(cli, bundle.env.base_image, build_cmd, bundle.built_image_tag)
    return C.ensure_image(cli, bundle.image)


_MAX_PARALLEL_TESTS = 8


def _grade(conn, cmd_id, cont, bundle, phase):
    """Run both buckets, log results, return (n_ok, n_total, per_bucket).

    Tests run concurrently (separate `docker exec` calls into the same
    container -- each test is its own process, no shared container state
    between them) via a thread pool; docker-py's exec_run blocks on I/O so
    threads parallelize this fine. Assumes tests are independent/parallel-safe
    (same assumption pytest-xdist makes) -- a test suite with real
    cross-test side effects (shared files, ports) isn't a case we handle
    specially. DB writes happen after, sequentially in the main thread --
    sqlite3 connections aren't safe for concurrent writes from multiple
    threads.

    per_bucket: {"fail2pass": (ok, total), "pass2pass": (ok, total)} -- so
    callers can report "fail2pass 5/5, pass2pass 1/1" instead of a combined
    number that hides which bucket actually mattered.
    """
    tasks = [(bucket, tid) for bucket, ids in (("fail2pass", bundle.fail2pass),
                                                ("pass2pass", bundle.pass2pass))
             for tid in ids]
    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_TESTS, len(tasks) or 1)) as pool:
        outcomes = list(pool.map(
            lambda t: R.run_test(cont, bundle.env.workdir, bundle.env.test_cmd, t[1]), tasks,
        ))

    ok = total = 0
    per_bucket = {}
    for (bucket, tid), outcome in zip(tasks, outcomes):
        expected = R.expected_outcome(bucket, phase)
        DB.add_test_result(conn, cmd_id, bucket, tid, phase, outcome, expected)
        b_ok, b_total = per_bucket.get(bucket, (0, 0))
        per_bucket[bucket] = (b_ok + int(outcome == expected), b_total + 1)
        total += 1
        ok += int(outcome == expected)
    return ok, total, per_bucket


def _bucket_summary(per_bucket: dict) -> str:
    return ", ".join(f"{bucket} {ok}/{total}" for bucket, (ok, total) in per_bucket.items())


@app.command()
def init(bundle_path: str = typer.Argument(..., help="Path to bundle dir")):
    """Ensure the task image is present and the harness runs."""
    bundle = Bundle.load(bundle_path)
    conn = DB.connect()
    cid = DB.start_command(conn, "init", bundle.task_id, {"bundle": bundle_path}, _now())
    try:
        cli = C.client()
        image_id = _ensure_image(cli, bundle)
        cont = C.start(cli, bundle.image, bundle.env.workdir)
        try:
            rc, out = C.exec_run(cont, "git rev-parse HEAD", workdir=bundle.env.workdir)
            if rc != 0:
                raise RuntimeError(f"repo not found at {bundle.env.workdir}:\n{out}")
            UI.print_status_line(True, f"image {bundle.image} ready; repo HEAD {out.strip()[:12]}")
        finally:
            cont.remove(force=True)
        DB.finish_command(conn, cid, status="success", image=image_id,
                          summary=f"image ready ({bundle.image})", finished_at=_now())
    except KeyboardInterrupt:
        DB.finish_command(conn, cid, status="interrupted", error="interrupted by user (Ctrl-C)",
                          finished_at=_now())
        raise
    except Exception as e:
        DB.finish_command(conn, cid, status="failed", error=str(e), finished_at=_now())
        UI.print_status_line(False, f"error: {e}  (run id {cid})")
        raise typer.Exit(1)


@app.command()
def validate(bundle_path: str = typer.Argument(...)):
    """Assert baseline: p2p pass, f2p fail (before any harness runs)."""
    bundle = Bundle.load(bundle_path)
    conn = DB.connect()
    cid = DB.start_command(conn, "validate", bundle.task_id, {"bundle": bundle_path}, _now())
    try:
        cli = C.client()
        image_id = _ensure_image(cli, bundle)
        cont = C.start(cli, bundle.image, bundle.env.workdir)
        try:
            C.apply_patch(cont, bundle.env.workdir, bundle.test_patch_text())
            ok, total, per_bucket = _grade(conn, cid, cont, bundle, "baseline")
        finally:
            cont.remove(force=True)
        status = "success" if ok == total else "failed"
        summary = f"baseline: {_bucket_summary(per_bucket)} as expected"
        DB.finish_command(conn, cid, status=status, image=image_id, finished_at=_now(),
                          summary=summary)
        UI.print_status_line(ok == total, f"{summary}  (run id {cid})")
        if ok != total:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        DB.finish_command(conn, cid, status="interrupted", error="interrupted by user (Ctrl-C)",
                          finished_at=_now())
        raise
    except Exception as e:
        DB.finish_command(conn, cid, status="failed", error=str(e), finished_at=_now())
        UI.print_status_line(False, f"error: {e}  (run id {cid})")
        raise typer.Exit(1)


_DEFAULT_SOLVER = {
    "mini-swe-agent": "ollama/qwen2.5-coder:7b",
    "opencode": "opencode/north-mini-code-free",
}


@app.command()
def run(
    bundle_path: str = typer.Argument(...),
    harness: str = typer.Option(None, help="stub | mini-swe-agent | opencode -- the driver/framework. "
                                            "Falls back to the bundle's task.json "
                                            "\"harness\" block, then \"stub\"."),
    solver: str = typer.Option(None, help="the LLM being benchmarked, in the harness's own id format "
                                           "(e.g. anthropic/claude-sonnet-4-5, opencode/north-mini-code-free). "
                                           "Falls back to the bundle's task.json \"harness\" block, then a "
                                           "hardcoded per-harness default."),
    api_base: str = typer.Option(None, "--api-base",
                                  help="custom OpenAI-compatible endpoint (e.g. GLM, Kimi/Moonshot). "
                                       "Use with --solver <provider-id>/<model-name>. Falls back to "
                                       "the bundle's task.json \"harness\" block."),
    api_key: str = typer.Option(None, "--api-key",
                                 help="paired with --api-base. Never logged or written to disk -- "
                                      "kept in memory and passed at exec time only. Never read "
                                      "from task.json -- CLI/env only."),
):
    """Invoke a harness (driven by --solver, the LLM under test), then grade
    the patched repo (post_solver phase).

    Precedence for harness/solver/api_base: CLI flag > bundle's task.json
    "harness" defaults > hardcoded fallback. api_key is CLI-only, never
    sourced from task.json (that file is meant to be committed/shared).
    """
    bundle = Bundle.load(bundle_path)
    defaults = bundle.harness_defaults
    harness = harness or defaults.harness or "stub"
    solver = solver or defaults.model or _DEFAULT_SOLVER.get(harness)
    api_base = api_base or defaults.api_base
    conn = DB.connect()
    # api_key deliberately excluded from args_json -- it must never land in the DB.
    args = {"bundle": bundle_path, "harness": harness, "solver": solver, "api_base": api_base}
    cid = DB.start_command(conn, "run", bundle.task_id, args, _now())
    hns = None
    try:
        hns = get_harness(harness)
        if harness == "stub":
            hns.patch_text = bundle.golden_patch_text()

        cli = C.client()
        image_id = _ensure_image(cli, bundle)
        cont = C.start(cli, bundle.image, bundle.env.workdir, network_disabled=not hns.needs_network)
        try:
            # Solve phase: gated tests NOT staged (hidden from the harness).
            ctx = HarnessContext(description=(bundle.root / "description.md").read_text(),
                                  workdir=bundle.env.workdir, model=solver,
                                  api_base=api_base, api_key=api_key)
            hns.solve(cont, ctx)
            solver_patch = C.capture_diff(cont, bundle.env.workdir)

            # Grading phase: stage gated tests, then run. Discard any harness
            # edits to files test_patch is about to overwrite first -- those
            # files aren't part of the graded solution, and a harness that
            # touched one (e.g. writing its own regression tests while
            # validating a fix) would otherwise make this apply fail on a
            # context mismatch.
            C.reset_files_touched_by(cont, bundle.env.workdir, bundle.test_patch_text())
            C.apply_patch(cont, bundle.env.workdir, bundle.test_patch_text())
            ok, total, per_bucket = _grade(conn, cid, cont, bundle, "post_solver")

            C.snapshot(cont, f"task-{bundle.task_id}:run-{cid}")
        finally:
            cont.remove(force=True)

        status = "success" if ok == total else "failed"
        summary = f"post_solver: {_bucket_summary(per_bucket)} passed"
        DB.finish_command(conn, cid, status=status, harness=harness, image=image_id,
                          solver_patch=solver_patch, transcript=getattr(hns, "transcript", None),
                          finished_at=_now(), summary=summary)
        UI.print_status_line(ok == total, f"harness={harness} solver={solver}: "
                                          f"{summary}  (run id {cid})")
    except KeyboardInterrupt:
        DB.finish_command(conn, cid, status="interrupted", harness=harness,
                          error="interrupted by user (Ctrl-C)",
                          transcript=getattr(hns, "transcript", None), finished_at=_now())
        raise
    except Exception as e:
        DB.finish_command(conn, cid, status="failed", harness=harness, error=str(e),
                          transcript=getattr(hns, "transcript", None), finished_at=_now())
        UI.print_status_line(False, f"error: {e}  (run id {cid})")
        raise typer.Exit(1)


@app.command()
def logs(
    command_id: int = typer.Argument(None, help="Command id to inspect"),
    list_: bool = typer.Option(False, "--list", help="List recent commands"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output, syntax-highlighted"),
    transcript: bool = typer.Option(False, "--transcript",
                                     help="Print the solver's full transcript "
                                          "(LLM responses, bash commands, output), colorized"),
    diff: bool = typer.Option(False, "--diff", help="Print just the solver's final patch, colorized"),
):
    """Query the log DB for a command's results."""
    conn = DB.connect()
    if list_ or command_id is None:
        rows = DB.list_commands(conn)
        if as_json:
            UI.print_json([dict(r) for r in rows])
        else:
            UI.print_commands_table(rows)
        return
    cmd, results = DB.get_command(conn, command_id)
    if not cmd:
        UI.print_status_line(False, f"no command with id {command_id}")
        raise typer.Exit(1)
    if transcript:
        UI.print_transcript(cmd["transcript"] or "(no transcript recorded for this run)")
        return
    if diff:
        UI.print_diff(cmd["solver_patch"] or "(no patch recorded for this run)")
        return
    if as_json:
        UI.print_json({"command": dict(cmd), "test_results": [dict(r) for r in results]})
        return
    UI.print_command_summary(cmd, results)


@app.command()
def analyse(
    command_id: int = typer.Argument(..., help="a `task run` id to analyse"),
    llm: str = typer.Option(..., "--llm", help="model to generate the report, e.g. "
                                                "openai/gpt-5.6, anthropic/claude-sonnet-4-5"),
    api_base: str = typer.Option(None, "--api-base", help="custom OpenAI-compatible endpoint for --llm"),
    api_key: str = typer.Option(None, "--api-key",
                                 help="paired with --api-base, or any provider's own key if its "
                                      "env var (e.g. OPENAI_API_KEY) isn't already set. Never logged."),
):
    """Generate a human-readable report over a stored `task run`: test
    results before/after, what the harness+solver actually did, and an
    assessment -- synthesized by an LLM from the transcript, patch, and
    before/after test results already in the DB. No Docker involved."""
    conn = DB.connect()
    cmd, after_results = DB.get_command(conn, command_id)
    if not cmd:
        UI.print_status_line(False, f"no command with id {command_id}")
        raise typer.Exit(1)
    if cmd["command"] != "run":
        UI.print_status_line(False, f"command {command_id} is a '{cmd['command']}', not a 'run' -- nothing to analyse")
        raise typer.Exit(1)

    baseline_cmd, baseline_results = A.get_baseline(conn, cmd["task_id"], command_id)
    prompt = A.build_prompt(cmd, after_results, baseline_cmd, baseline_results)

    # api_key deliberately excluded from args_json -- same rule as `task run`.
    args = {"command_id": command_id, "llm": llm, "api_base": api_base}
    aid = DB.start_command(conn, "analyse", cmd["task_id"], args, _now())
    try:
        report = A.call_llm(llm, prompt, api_base, api_key)
        DB.finish_command(conn, aid, status="success", finished_at=_now(),
                          summary=f"analysed run {command_id} with {llm}")
        UI.console.print(Markdown(report))
    except KeyboardInterrupt:
        DB.finish_command(conn, aid, status="interrupted", error="interrupted by user (Ctrl-C)",
                          finished_at=_now())
        raise
    except Exception as e:
        DB.finish_command(conn, aid, status="failed", error=str(e), finished_at=_now())
        UI.print_status_line(False, f"error: {e}  (run id {aid})")
        raise typer.Exit(1)


@app.command(name="bundles")
def list_bundles(
    root: str = typer.Argument("bundles", help="directory to scan (each immediate "
                                                 "subdirectory with a task.json counts)"),
):
    """List available task bundles: task_id, repo, fail2pass/pass2pass counts,
    harness default -- no Docker involved, just reads task.json files."""
    found, errors = B.discover(root)
    if not found and not errors:
        UI.print_status_line(False, f"no bundles found under {root}")
        raise typer.Exit(1)
    UI.print_bundles_table(found, errors)


if __name__ == "__main__":
    app()
