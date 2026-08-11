# Design Notes

## Overview

`task` is a CLI that runs SWE-bench-style coding evals in an isolated,
reproducible way. A **bundle** defines one task (repo, commit, problem
statement, golden patch, gated test ids). Three commands form a pipeline:
`init` (build/pull the task image) → `validate` (prove the baseline is
well-formed) → `run` (invoke a harness, driven by a solver -- the LLM under
test -- and grade the result). A fourth command, `analyse`, turns a stored
run into a synthesized human-readable report. Every invocation is logged to
SQLite so collaborators query a run id instead of reproducing it.

## Bundle & `task.json`

```jsonc
{
  "task_id": "ansible__ansible-d9f1866",
  "repo": "https://github.com/ansible/ansible",
  "base_commit": "59ca05b70994b07a9507f61a0871146a4991b262",
  "dockerhub_tag": "ansible.ansible-ansible__ansible-d9f1866...-v0f01c...",

  "env": {
    "base_image": null,                 // null → pull jefzda/sweap-images:<dockerhub_tag>
    "build_cmd": null,                  // null → image already built; else run to install deps
    "test_cmd": "python -m pytest -q {test_id}",
    "workdir": "/app"                   // repo path inside image (verified at init)
  },

  "test_patch": "test_patch.diff",      // stages gated test files; applied only in grading phase
  "golden_patch": "patch.diff",         // the fix; applied only by the stub solver

  "fail2pass": ["test/units/.../test_check_type_dict.py::test_check_type_dict_fail"],
  "pass2pass": ["test/units/.../test_check_type_dict.py::test_check_type_dict"],

  "timeout_sec": 1800
}
```

**Test buckets are node-id lists, not folders.** Real p2p and f2p tests often
live in the same file (split per-function), so the problem statement's
`tests/pass2pass/` `tests/fail2pass/` folder sketch can't represent them.
Bucket membership is metadata; test files live wherever the repo puts them.

**Image default.** If `env.base_image` is null, `init` pulls the prebuilt
SWE-bench Pro image `jefzda/sweap-images:<dockerhub_tag>` — repo already inside
at `base_commit`. Override by setting `base_image` + `build_cmd`.

**Arbitrariness.** `test_cmd` is declared per-bundle with a `{test_id}`
placeholder, so swapping pytest for `go test` / `jest` is a bundle change, not
a code change.

## Command pipeline

```
init:     ensure image present (pull dockerhub_tag OR build base_image+build_cmd);
          start a throwaway container, locate workdir, smoke-run test_cmd to
          confirm the harness works; record resolved image digest.

validate: fresh container from image → apply test_patch → run p2p+f2p →
          assert p2p all pass, f2p all fail. Baseline guardrail.

run:      fresh container from image → harness+solver edits repo (test_patch NOT
          applied, so gated tests are hidden) → capture git diff → apply test_patch →
          run p2p+f2p → grade → commit container to task-<id>:run-<cmd_id> for debug.

analyse:  no container at all -- pure DB read + one LLM call over an existing
          run's stored transcript/patch/test results, synthesized into a report.

bundles:  no container, no DB -- scans a directory for task.json files and
          lists them (task_id, repo, fail2pass/pass2pass counts, harness
          default). A malformed bundle is skipped with a message, not a
          crash -- one bad task.json shouldn't hide every other bundle.
```

**One image per task, throwaway container per command.** The repo lives in the
*image*, not a long-lived container, so every command starts from a pristine
baseline. The solver's edits die with its container — clean isolation and
determinism. (See "container lifecycle" reasoning: a single long-lived
container would let a `run`'s mutations pollute a later `validate`.)

## Hiding gated tests from the harness

Mechanism is **ordering, not sandboxing gymnastics.** In SWE-bench the gated
tests come *from* `test_patch` — they don't exist at `base_commit`. So:

- Solve phase: repo at base_commit, `test_patch` NOT applied → gated tests
  simply absent. The harness still sees pre-existing (non-gated) tests, which
  it is allowed to.
- Grading phase: capture the harness's diff, THEN apply `test_patch`, THEN
  run gated tests.

Pre-existing p2p tests visible to the harness is correct — those are
regression tests it's allowed to see. Only the new assertions (via
test_patch, mostly f2p) must be hidden, and they are by construction.

**Observed for real, then fixed**: a capable harness+solver (GPT-5.6 via
mini-swe-agent) rewrote a gated test file itself while adding its own
regression tests during solving, so `git apply` on `test_patch` failed with
a context mismatch against the harness's version of that file. Since gated
test files aren't part of the graded solution — `test_patch` is about to
overwrite them regardless — `container.reset_files_touched_by()` discards
any local changes to exactly the files `test_patch` names (parsed from its
own `diff --git a/... b/...` headers) immediately before staging it. Only
those specific paths are reset; the harness's actual source-code fix, in
other files, is untouched. Reproduced the original failure in isolation
(wrote a divergent file, confirmed `git apply` failed) and confirmed the fix
resolves it before shipping.

## Harnesses (pluggable) and solvers (the LLM under test)

A **harness** is the driver/framework; a **solver** is the actual LLM it's
driven by (`--solver anthropic/claude-sonnet-4-5`, `ollama/qwen2.5-coder:7b`,
`opencode/north-mini-code-free`, ...) — the thing this tool exists to
benchmark. `Harness` interface: given (description, repo_path,
visible_tests), mutate the repo in place; caller diffs afterward.

- **stub** — `git apply patch.diff`, ignores `--solver`. Zero cost, proves
  harness/grading correctness, used for the end-to-end demo.
- **mini-swe-agent** (default real harness) — pure-Python agent loop
  (think→bash→observe) inside the container, any litellm-supported solver
  (local Ollama by default). Transparent, easy to log/debug.
- **opencode** — Go binary, subprocess wrapper, headless mode, any
  opencode-supported solver (free-tier by default). Demonstrates
  pluggability across a process boundary.

Patch application is harness-dependent: agents edit in place (no apply
step); stub applies an external diff. `task run` treats it as the harness's
contract. Regardless of harness, `git diff` is captured right after it
finishes → the evaluation artifact's "solver patch" + a debugging aid (diff
vs golden patch).

## Database (SQLite)

```sql
commands(id, command, task_id, args_json, status, harness, image,
         solver_patch, transcript, summary, error, started_at, finished_at, duration_sec)
test_results(id, command_id→commands, bucket, test_id, phase,
             outcome, expected, passed_expectation, duration_sec)
```

- `passed_expectation` (1/0) = the real success signal: outcome == expected for
  that bucket+phase. validate → p2p:pass, f2p:fail. run post_solver → both:pass.
- `solver_patch` stored inline so a run id query is self-contained.
- `transcript` stores the harness's full think→act→observe log (LLM
  responses, bash commands, diffs) — visibility into *how* it got to that
  patch, not just the end state.
- `run` stores only post_solver rows; baseline is `validate`'s job (not
  re-run) to keep runs cheap.
- Query surface: `task logs <id>` (row + its test_results; `--json` for raw,
  `--diff`/`--transcript` for colorized views), `task logs --list` (recent
  commands, as a table).

## `task analyse`: LLM-synthesized reports over stored runs

Since every run's transcript/patch/test results already live in the DB,
`task analyse <run-id> --llm <model>` builds a prompt from them (before-run
results from the most recent prior `validate` for the same `task_id`,
after-run results, the final patch, the full transcript) and makes one
one-shot `litellm.completion()` call to synthesize a markdown report. No
Docker involved -- pure DB read + one LLM call. The `--llm` judge is
deliberately independent of the `--harness`/`--solver` that produced the
run (same `--api-base`/`--api-key` convention as `task run`). Finding the
"before" baseline is a heuristic (latest prior `validate`, same `task_id`,
no explicit FK) -- good enough since a bundle is normally validated once
before any runs against it.

## Robustness fixes found by actually using the tool

Two real bugs surfaced through live use, not code review, and both are
fixed:

- **Missing credentials caused a ~60s retry loop, not a clear error.**
  mini-swe-agent's own retry wrapper aborts immediately on
  `litellm.exceptions.AuthenticationError` -- but litellm classifies a
  missing OpenAI key as `InternalServerError` instead, which isn't on that
  abort list, so it retried 10x with exponential backoff before the real
  error surfaced. Fixed by checking credentials ourselves
  (`litellm.validate_environment()`) before starting the agent loop --
  fails in ~2s with an actionable message instead.
- **Ctrl-C left the DB row stuck in `running` forever.** All four commands
  only caught `Exception`, and `KeyboardInterrupt` is a `BaseException`, not
  an `Exception` -- so interrupting a run left no record of what happened.
  All four now catch `KeyboardInterrupt` explicitly and mark the row
  `interrupted` before re-raising.
- **Found while verifying the fix above**: failures printed nothing to the
  console at all -- only `task logs` would reveal why. Every failure path
  now prints `error: <message> (run id <id>)` directly.

## Tech stack

- **CLI**: Typer (subcommands, type-hinted args, auto `--help`).
- **Containers**: Docker SDK for Python (structured errors over shell-out).
- **DB**: stdlib `sqlite3`, no ORM.
- **Packaging**: `uv` + `pyproject.toml` + `uv.lock` (reproducibility);
  `[project.scripts]` installs the `task` command.
- **Solver backend**: local Ollama or opencode's free tier by default (no
  paid API); swappable via litellm (mini-swe-agent) or opencode's own
  provider config, including any OpenAI-compatible endpoint via `--api-base`.
- **Output**: `rich` for colored diffs, syntax-highlighted JSON, tables.

## Testing our own CLI

- Small `pytest` over pure logic: test-output parsing, `passed_expectation`,
  task.json defaults, DB write/query roundtrip.
- `task run --harness stub` on the example bundle = live integration test AND
  the demo (golden patch flips f2p to pass, p2p stays pass).
- Skipped: mocking Docker/Ollama, coverage gates, per-function suites (YAGNI).

## Demo instances

`instance_ansible__ansible-d9f1866...` — 1 f2p + 1 p2p in a single file
(`test/units/module_utils/common/validation/test_check_type_dict.py`), pure
`module_utils` type-validation logic, no DB/network. Backup:
openlibrary `03095f2` (2 f2p / 1 p2p).

`instance_ansible__ansible-83909bfa...` (`bundles/ansible-galaxy-login-removal`)
— a deliberately bigger/scale instance: removing `ansible-galaxy login` and
migrating to API-token auth, spanning CLI arg parsing, module removal, docs.
1 f2p + 128 p2p (after cleanup, see below) — chosen specifically to stress
parallel grading at scale and to need several minutes of real multi-file
harness+solver reasoning, not a one-line fix.

### Real upstream data-quality bugs found building it

Building this bundle from the dataset's raw `PASS_TO_PASS` (150 ids)
surfaced three genuine issues in SWE-bench Pro's export, not our parsing:

1. **Truncated node ids** — 17 long, multi-line parametrized ids (YAML
   values as pytest ids, e.g. `test_parse_requirements[\ncollections:\n- ...]`)
   were cut off mid-string in the raw HF field itself (confirmed by
   inspecting the raw field text). Filtered via a bracket-count mismatch
   check (`tid.count('[') != tid.count(']')`).
2. **Collapsed URL slashes** — 10 ids with an embedded URL had `https://`
   reduced to `https:/` somewhere in the dataset's own pipeline, so they
   never matched pytest's real collected ids. Cross-checked against
   `pytest --collect-only`'s actual output and corrected.
3. **Environment-sensitive assertions** — 4 tests assert an exact mock call
   count that depends on this 2020-era code's interaction with *current*
   `resolvelib`/`PyYAML` (an extra legitimate warning fires on modern
   versions) — real behavior, not a data bug, but not reproducible across
   environments. Excluded rather than chasing period-exact dependency pins,
   since `validate`'s entire purpose is a deterministic baseline signal.

Net: 150 → 128 clean ids, `validate` now green (`fail2pass 1/1, pass2pass
128/128`), 129 tests concurrently in ~15s. This is exactly the "arbitrary
real-world repo" messiness a harness has to tolerate, not paper over --
worth surfacing rather than silently dropping bad ids without a trace.

## Performance: parallel test execution

`_grade()` runs every fail2pass/pass2pass test as a concurrent `docker exec`
into the same container (`ThreadPoolExecutor`, capped at 8) instead of
sequentially. Each test is its own process; docker-py's exec_run blocks on
I/O so threads parallelize this for free. DB writes still happen
sequentially afterward, in the main thread — sqlite3 isn't safe for
concurrent writes from multiple threads. Assumes tests are
independent/parallel-safe, same assumption pytest-xdist itself makes; a
suite with real cross-test side effects (shared files, ports) isn't
special-cased. Measured: 6-test bundle validate in ~1.3s wall-clock.

## Deferred / bonus

Observability (per-stage snapshots — partially via run-snapshot image),
resource limits (CPU/memory/pids caps) on solver containers — network-off is
handled, resource caps deliberately skipped for this scope. Marked with
`ponytail:` comments where a shortcut has a known upgrade path.
