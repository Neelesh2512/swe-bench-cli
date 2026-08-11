# SWE-bench CLI

Run SWE-bench-style coding evals in isolated, reproducible containers.
A **bundle** = one task (repo, commit, problem statement, golden patch, gated
tests). A **harness** (`stub` / `mini-swe-agent` / `opencode`) is the
driver/framework; a **solver** is the actual LLM being benchmarked
(e.g. `anthropic/claude-sonnet-4-5`) — the thing this tool exists to measure.
Every invocation is logged to SQLite, queryable by id.

Full design rationale and tradeoffs: [`docs/DESIGN.md`](docs/DESIGN.md).

## Setup

**Requirements**
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Docker (or a compatible engine — OrbStack, Docker Desktop, Colima) running locally

**Install**
```bash
git clone <this repo>
cd swe-bench-cli
uv sync
```
Run the CLI via `uv run task ...`. `--harness mini-swe-agent` needs one more
extra: `uv sync --extra agent`. `--harness stub`/`opencode` need nothing else
(opencode's binary installs inside the container image, not on the host).

## Commands

| command | what it does |
|---|---|
| `task bundles [dir]` | List available bundles (task_id, repo, test counts, harness default). No Docker. |
| `task init <bundle>` | Build/pull the task image, verify the harness can run in it. |
| `task validate <bundle>` | Guardrail: assert fail2pass all fail / pass2pass all pass on the untouched baseline. |
| `task run <bundle> --harness <h> --solver <model>` | Invoke a harness+solver, then grade fail2pass/pass2pass again. |
| `task logs [id]` | Query the log DB — `--list`, `--json`, `--diff`, `--transcript`. |
| `task analyse <run-id> --llm <model>` | LLM-synthesized human-readable report over a stored run. |

Full usage detail: solvers/providers/harness config in
[`docs/HARNESSES.md`](docs/HARNESSES.md); bundle format and the included
example bundles in [`docs/BUNDLES.md`](docs/BUNDLES.md).

## Quickstart

```bash
uv run task bundles
uv run task init bundles/ansible-check-type-dict
uv run task validate bundles/ansible-check-type-dict
uv run task run bundles/ansible-check-type-dict --harness stub
uv run task logs --list
```

Expected:
- `validate` → `baseline: fail2pass 1/1, pass2pass 1/1 as expected`
- `run --harness stub` → `harness=stub solver=...: post_solver: fail2pass 1/1,
  pass2pass 1/1 passed` (fail2pass flips fail→pass, pass2pass stays pass)

`stub` applies the bundle's golden patch directly — no LLM, no reasoning. It
proves the harness/grading is correct; swap in `--harness opencode` for a
real LLM run (see [`docs/HARNESSES.md`](docs/HARNESSES.md)).

All output is rendered with [`rich`](https://github.com/Textualize/rich) —
colored diffs, syntax-highlighted JSON, tables. Colors auto-disable when
piped/redirected, same as `git`/`ls`.

## Development

```bash
uv run pytest
```
Pure-logic tests (no Docker needed) — test-outcome expectations, bundle
loading, DB roundtrip. `task run --harness stub` on an example bundle is the
integration test; run it manually to confirm the live pipeline end to end.

**Layout**: `src/swe_bench_cli/` — `cli.py` (commands), `bundle.py`
(task.json loading), `container.py` (Docker), `db.py` (SQLite), `harnesses/`
(pluggable drivers), `analyse.py`, `display.py` (rich output). `bundles/`
holds example task bundles; `docs/` holds design notes and usage guides.

## Further reading

- [`docs/DESIGN.md`](docs/DESIGN.md) — architecture, tradeoffs, bugs found
  and fixed by actually using the tool.
- [`docs/TECH_STACK.md`](docs/TECH_STACK.md) — why each dependency/choice.
- [`docs/HARNESSES.md`](docs/HARNESSES.md) — harnesses, solver/provider
  config, per-bundle defaults, `task analyse`.
- [`docs/BUNDLES.md`](docs/BUNDLES.md) — `task.json` schema, writing a
  bundle, the included examples, known limitations.
