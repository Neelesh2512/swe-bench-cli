# Writing a Bundle

A bundle is a directory with:

```
my-task/
  task.json          # metadata — see schema below
  description.md      # problem statement (never shows p2p/f2p test content)
  patch.diff           # golden fix (unified diff)
  test_patch.diff      # diff that stages the gated test files
```

Run `uv run task bundles` to list every bundle found under a directory
(default `./bundles`) with its task_id, repo, fail2pass/pass2pass counts, and
harness default — no Docker needed, it just reads `task.json` files.

## `task.json` schema

```jsonc
{
  "task_id": "unique-id",
  "repo": "https://github.com/org/repo",
  "base_commit": "<sha>",
  "dockerhub_tag": null,          // set if a prebuilt SWE-bench Pro image exists for this instance

  "env": {
    "test_cmd": "python -m pytest -q {test_id}",   // {test_id} is substituted per test
    "workdir": "/app",                              // repo path inside the container
    "base_image": "python:3.11-slim",                // set to build a custom image...
    "build_cmd": "git clone --filter=blob:none {repo} {workdir} && cd {workdir} && git checkout {base_commit} && pip install ..."
                                                     // ...run once by `init`, then cached
  },

  "test_patch": "test_patch.diff",
  "golden_patch": "patch.diff",
  "fail2pass": ["path/to/test.py::test_name"],       // must fail baseline, pass after a real fix
  "pass2pass": ["path/to/test.py::other_test"],      // must pass both before and after
  "timeout_sec": 1800,

  "harness": {                                       // optional -- see docs/HARNESSES.md
    "harness": "opencode",                             // CLI flag always overrides this
    "model": "opencode/north-mini-code-free",            // the solver
    "api_base": null                                    // never api_key -- CLI/env-only, always
  }
}
```

Notes:
- **Image resolution**: if `env.base_image` is set, `init` builds and tags a
  local image once (`task-<id>:base`) and reuses it on later commands. If
  unset, `init` pulls `jefzda/sweap-images:<dockerhub_tag>` (SWE-bench Pro's
  prebuilt images) instead.
- **Test buckets are node-id lists, not folders** — real p2p/f2p tests often
  live in the same file, split per test function, so they can't be
  represented as separate directories.
- **Gated tests are hidden by ordering, not sandboxing**: the harness only
  ever sees the repo before `test_patch` is applied. Grading applies
  `test_patch` afterward, then runs the gated tests.
- **Network**: containers run with network disabled by default (isolation).
  `opencode` is the exception — it makes its own API calls from inside the
  container, so `task run --harness opencode` starts that container with
  network enabled (see `Harness.needs_network`). If you want `opencode`
  available for your own bundle, bake its install into `build_cmd` (see the
  included bundles' `task.json` for the exact line) so it's cached in the
  built image instead of re-downloaded every run.

## Known limitation: SWE-bench Pro's prebuilt images on Apple Silicon

`jefzda/sweap-images:*` are amd64-only. On an Apple Silicon Mac we found no
combination of OrbStack Rosetta or qemu emulation that could exec **any**
binary from these specific images (`cannot execute binary file`), even
though plain amd64 images (`ubuntu`, `python`, `alpine`) emulate fine on the
same machine — apparently something specific to how those images were built.
This is why the example bundles use the `base_image` + `build_cmd` path
(a native arm64 image, built locally) instead of the prebuilt tag, even
though `dockerhub_tag` is recorded in `task.json` for reference. On an x86
Linux/Mac host, the prebuilt path should work directly by setting
`base_image` to `null`.

## A note on this dataset's data quality

Building `bundles/ansible-galaxy-login-removal/` (150 raw `PASS_TO_PASS` ids
from the dataset) surfaced three genuine upstream data-quality issues in the
SWE-bench Pro export for this instance — worth documenting since they'd
silently corrupt grading if unnoticed:

1. **Truncated node ids.** 17 parametrized test ids with long, multi-line
   YAML values (e.g. `test_parse_requirements[\ncollections:\n- ...]`) were
   cut off mid-string in the raw dataset field — confirmed by inspecting the
   raw HF field text directly, not a parsing bug on our end. Filtered out
   (bracket-count mismatch: `tid.count('[') != tid.count(']')`).
2. **Collapsed URL slashes.** 10 ids embedding a URL had `https://` reduced
   to `https:/` (single slash) somewhere in the dataset's pipeline, so they
   never matched pytest's real collected node ids. Fixed by cross-checking
   against `pytest --collect-only`'s actual output and correcting the ids.
3. **Environment-sensitive assertions.** 4 remaining tests assert an exact
   mock call count that depends on this 2020-era codebase's interaction with
   *current* `resolvelib`/`PyYAML` versions (an extra legitimate warning
   fires on modern dependency versions) — not a data bug, but not
   reproducible across environments either. Excluded rather than chasing
   period-exact dependency pins, since `task validate`'s whole point is a
   deterministic baseline signal.

Net: 150 → 128 clean `pass2pass` ids. This is exactly the kind of
"arbitrary real-world repo" messiness the harness has to tolerate — see
`docs/DESIGN.md` for how each was diagnosed.

## The included example bundles

Three real instances pulled from
[SWE-bench Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro),
different bugs/files/scale in the same repo — proves the harness generalizes
beyond one small task:

- `bundles/ansible-check-type-dict/` — bug in `check_type_dict`'s validation
  helper. 1 fail2pass test, 1 pass2pass test.
- `bundles/ansible-unarchive-timestamp/` — bug in the `unarchive` module's
  ZIP timestamp parsing. 5 fail2pass tests (parametrized), 1 pass2pass test.
- `bundles/ansible-galaxy-login-removal/` — a much bigger task: removing the
  `ansible-galaxy login` command entirely and migrating to API-token auth,
  spanning CLI arg parsing, module removal, and docs. 1 fail2pass, **128
  pass2pass** — a real stress test for parallel grading (129 concurrent
  `docker exec` calls validate in ~15s) and for a harness that needs several
  minutes of real multi-file reasoning, not a one-line fix. See "A note on
  this dataset's data quality" above — three genuine upstream data bugs were
  found and fixed while building this bundle.

Saved artifacts from real runs against these bundles:
- `bundles/ansible-check-type-dict/artifacts/run-63-stub-solver.json`
- `bundles/ansible-unarchive-timestamp/artifacts/run-64-stub-solver.json`
- `bundles/ansible-galaxy-login-removal/artifacts/run-70-stub-solver.json`
- `bundles/ansible-check-type-dict/artifacts/run-37-opencode-solver.json` — a
  **real LLM run**, see `docs/HARNESSES.md`.
