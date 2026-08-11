# Tech Stack & Architecture Decisions

## CLI framework
**Typer.** Subcommand routing, type-hint-based args, auto `--help`. Chosen over
raw `argparse` for clearer UX and better error messages.
Chosen over Click directly since Typer is a thinner layer on top with less
boilerplate.

## Containerization
**Docker SDK for Python** (`docker` package), not raw `subprocess` + CLI
shell-out. Reasons: structured exceptions, easier exit-code/log capture,
cleaner error messages back to the user.

## Database
**stdlib `sqlite3`.** No ORM — schema is small (commands log + test results),
SQLAlchemy would be over-engineering for this scope. Two tables:
- `commands`: id, command name, args, timestamp, status
- `runs` (or `test_results`): run id, per-test pass/fail, linked to a command id

## Harness interface (pluggable, solver-agnostic)

A **harness** is the driver/framework (opencode, mini-swe-agent, stub); a
**solver** is the actual LLM it's driven by, passed as `--solver <id>` — the
thing this tool exists to benchmark, kept orthogonal to the harness.

```python
class Harness(ABC):
    def solve(self, container, ctx: HarnessContext) -> None:
        """Mutates the repo in place. Caller diffs it afterward to get the patch.
        ctx.model carries the solver id (e.g. "anthropic/claude-sonnet-4-5")."""
```

Implementations:
- **`stub`** — applies `patch.diff` (the golden patch) directly via `git apply`,
  ignores `--solver`. Zero cost, proves harness correctness. Used for the
  end-to-end demo.
- **`mini-swe-agent`** — default real harness. Pure-Python library, called
  in-process. Runs an agent loop (think → bash action → observe) **inside the
  container**, giving it file read/write + bash access to iteratively fix the
  repo. Solver-agnostic via litellm backend; any provider litellm supports
  (local Ollama, Anthropic, OpenAI, Gemini, or any OpenAI-compatible endpoint
  via `--api-base`). Chosen as default: minimal surface area, transparent
  trajectory logging, easiest to explain/debug in design notes.
- **`opencode`** — second harness, demonstrates pluggability with a
  stronger real-world coding agent. Go binary, invoked via `subprocess`
  (not in-process — it's an external tool being benchmarked, same as any
  LLM would be) in its non-interactive/headless run mode, inside the
  container. Solver-agnostic: opencode's own free tier by default, or any
  named provider / OpenAI-compatible endpoint via `--api-base`. Wrapper class
  just shells out and waits for exit; repo diff captured the same way as
  any other harness.

Why not SWE-agent / Aider / OpenHands for the default: SWE-agent is heavier
(YAML config, ACI abstraction) than needed for this scope. Aider is built for
interactive/chat use, more friction to run headless. OpenHands brings its own
sandboxed runtime, which would nest awkwardly inside our own container design.

### Patch application is harness-dependent

- `mini-swe-agent` edits files **in place** inside the container — no
  separate "apply patch" step needed before re-running tests.
- `stub` receives an external `patch.diff` and needs an explicit `git apply`
  step.

`task run` treats patch application as part of the harness's contract, not a
fixed pipeline step.

### Patch capture for artifacts/debugging

Regardless of harness, immediately after it finishes (before running tests),
capture `git diff` of the repo state. This becomes:
- the evaluation artifact's recorded "solver patch"
- a debugging aid (diff vs golden `patch.diff`)

Also captured: a full `transcript` (every LLM response, bash command, and
edit diff, in order) — visibility into *how* the harness+solver got there,
not just the final state. Surfaced via `task logs <id> --transcript`.

## Solver backend
**Local Ollama or opencode's free tier by default.** No paid tool required.
`mini-swe-agent` is configured with a solver at run time
(`--solver ollama/qwen2.5-coder:7b` or any litellm-supported provider).
Swapping providers is a config change on the litellm layer (or opencode's own
provider config), not a rewrite.

## Test runner (arbitrariness)
Test execution command is declared per-bundle in `task.json` (not hardcoded
to pytest), e.g. `"test_cmd": "pytest {test_path}"`. CLI execs whatever the
bundle declares inside the container. Keeps the core loop language-agnostic
even though the demo bundle is Python/pytest.
