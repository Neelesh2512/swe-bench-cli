"""task analyse: LLM-generated human-readable report over a stored run.

Pure read + one-shot LLM call -- no Docker involved. Reuses litellm (already
an `agent`-extra dependency) so any provider works via the same
model-id/--api-base/--api-key convention as `task run`.
"""

from __future__ import annotations

from . import db as DB


def _fmt_results(results) -> str:
    if not results:
        return "(none recorded)"
    lines = []
    for r in results:
        mark = "OK" if r["passed_expectation"] else "XX"
        lines.append(f"  {mark} {r['bucket']:<10} {r['outcome']:<7} "
                      f"(expected {r['expected']})  {r['test_id']}")
    return "\n".join(lines)


def get_baseline(conn, task_id: str, before_id: int):
    """Most recent `validate` command for this task_id, run before `before_id`.

    There's no explicit foreign key linking a `run` to the `validate` that
    preceded it -- this is a heuristic (latest prior validate, same task_id).
    Good enough given `validate` is normally run once per bundle before any
    `run`, but a bundle re-validated between two `run`s could pick the wrong
    one; flagged in the report if no baseline is found at all.
    """
    row = conn.execute(
        "SELECT id FROM commands WHERE command='validate' AND task_id=? AND id<? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, before_id),
    ).fetchone()
    if not row:
        return None, []
    return DB.get_command(conn, row["id"])


def build_prompt(cmd, after_results, baseline_cmd, baseline_results) -> str:
    before_section = (_fmt_results(baseline_results) if baseline_cmd
                      else "(no prior `task validate` found for this task_id)")
    patch = cmd["solver_patch"] or "(no patch recorded)"
    transcript = cmd["transcript"] or "(no transcript recorded)"

    return f"""You are reviewing one run of an automated coding-agent eval harness.

Task: {cmd['task_id']}
Harness: {cmd['harness']}
Status: {cmd['status']}
Summary: {cmd['summary']}

## Before run (baseline test results, from `task validate`)
{before_section}

## After run (post-solver test results)
{_fmt_results(after_results)}

## Final patch produced
```diff
{patch}
```

## Full harness transcript (bash commands, LLM responses, file edits)
{transcript}

---
Write a concise, human-readable markdown report with exactly these sections:
# Test Report
## Before Run
## After Run
## What the Model Did
## Assessment

Be specific: name the files/functions actually touched, quote the key part
of the fix, explain concretely why any test still fails or passes, and end
with a one-line verdict. Do not repeat the raw transcript verbatim --
synthesize it."""


def call_llm(llm: str, prompt: str, api_base: str | None, api_key: str | None) -> str:
    try:
        import litellm
    except ImportError:
        raise RuntimeError("task analyse needs litellm -- run `uv sync --extra agent`") from None

    kwargs = {}
    if api_base:
        kwargs["api_base"] = api_base
    if api_key:
        kwargs["api_key"] = api_key
    response = litellm.completion(model=llm, messages=[{"role": "user", "content": prompt}], **kwargs)
    return response.choices[0].message.content
