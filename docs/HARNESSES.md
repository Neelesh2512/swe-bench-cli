# Harnesses, Solvers & Providers

A **harness** (`stub` / `mini-swe-agent` / `opencode`) is the driver/framework
— bash access, file edits, the agent loop. A **solver** is the actual LLM
being benchmarked (e.g. `anthropic/claude-sonnet-4-5`,
`opencode/north-mini-code-free`) — the thing this whole tool exists to
measure. `task run --harness opencode --solver anthropic/claude-sonnet-4-5`
runs opencode's agent loop, driven by Claude.

## Available harnesses

`task run --harness <name> --solver <model>`:

| harness | status | what it does |
|---|---|---|
| `stub` | working | applies the bundle's golden `patch.diff` directly, ignores `--solver` |
| `opencode` | working (real LLM) | external Go coding agent, installed into the image and run headless (`--auto`) inside the container. Default `--solver opencode/north-mini-code-free` — no API key, no paid tool |
| `mini-swe-agent` | wired, weak on tiny local models | agent loop (bash + file access); the LLM call happens in our host process via litellm, only bash actions run in the container. Default `--solver ollama/qwen2.5-coder:7b` |

No harness needs a paid API by default.

```bash
uv run task run bundles/ansible-check-type-dict --harness opencode
```

On the included bundle this produced a real, coherent partial fix across
several independent runs: it correctly deprecates `safe_eval` in both
required places but consistently misses the `literal_eval` fallback branch,
so 1 of 2 tests passes — no regression (pass2pass stays green). One run's
own final message even claimed "All tests pass" while our independent
grading caught that only 1/2 actually did — exactly the kind of
self-report-vs-ground-truth gap a harness needs to catch rather than trust.
See `bundles/ansible-check-type-dict/artifacts/run-37-opencode-solver.json`
for the full patch and test results.

## What `stub` actually does

It does not solve anything — it applies the bundle's `patch.diff` (the known
correct fix) directly via `git apply`, with no reasoning or LLM call. It
exists to prove the harness (containers, test hiding, grading) is correct,
independent of solver quality. A real harness+solver plugs into the same
interface and gets graded by the exact same downstream code.

## `opencode` specifics

**Free-tier gateway can stall.** opencode's hosted free solvers occasionally
stop responding mid-session on a single request, with no error — confirmed
via opencode's own log showing a request sent and never answered, reproduced
across multiple runs and two different free models. `OpenCodeHarness` runs it
under a small watchdog script (`.__run_opencode.sh`, written into the
container) that kills the process after **90 seconds with zero new activity
in opencode's own log** — not after a fixed total duration — so a bundle
that's genuinely still working isn't punished, while a stall is caught in
under two minutes instead of blocking indefinitely. Whatever was edited
before the stall is still captured and graded normally.

(We also tried allocating opencode a pseudo-terminal (`tty=True`) suspecting
a terminal-I/O hang; kept it since it's harmless, but it turned out not to
be the actual cause — the idle-watchdog is the real fix.)

## `mini-swe-agent` specifics

Requires a local Ollama model (`ollama pull qwen2.5-coder:7b` or similar,
`ollama serve` running) unless you point `--solver` at a hosted provider
instead (see below). In testing, a 1.5B local solver couldn't reliably
follow the "exactly one bash action per response" format (produced 5-7 code
blocks per response, hit a format-error limit, contributed zero edits) and a
7B solver was too slow to be practical on a laptop without a GPU. The wiring
is correct and reusable with a stronger/faster solver (a hosted API, or a
machine with a GPU) — it's a model-capability/speed tradeoff, not a harness
bug.

Needs the optional `agent` extra: `uv sync --extra agent`.

## Using GPT, Claude, Gemini, or any other solver

Both real harnesses are already solver-generic — which LLM you benchmark is
a `--solver` flag + an API key, not a code change.

**`mini-swe-agent`** — model call happens in *our host process* via
`litellm`, so any provider litellm supports works with zero container
changes:

```bash
export ANTHROPIC_API_KEY=sk-...
uv run task run <bundle> --harness mini-swe-agent --solver anthropic/claude-sonnet-4-5-20250929

export OPENAI_API_KEY=sk-...
uv run task run <bundle> --harness mini-swe-agent --solver openai/gpt-4o

export GEMINI_API_KEY=...
uv run task run <bundle> --harness mini-swe-agent --solver gemini/gemini-2.0-flash
```
Solver id format: `<litellm-provider>/<model-name>` — see the
[litellm providers list](https://docs.litellm.ai/docs/providers).

**`opencode`** — runs *inside the container* and makes its own API calls, so
it needs the key available there too. `OpenCodeHarness` forwards a fixed set
of provider env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`GOOGLE_GENERATIVE_AI_API_KEY`, `OPENROUTER_API_KEY`) from your host into the
container **at exec time only** — never baked into the image or a
run-snapshot, so a key can't leak into a stored artifact:

```bash
export ANTHROPIC_API_KEY=sk-...
uv run task run <bundle> --harness opencode --solver anthropic/claude-sonnet-4-5-20250929
```
Just set whichever key(s) you need on the host before running — nothing else
to configure. To add a provider we don't forward yet, add its env var name to
`_PROVIDER_ENV_VARS` in `harnesses/opencode.py`.

### Any OpenAI-compatible endpoint (GLM/Zhipu, Kimi/Moonshot, a self-hosted server, ...)

Providers without a named integration usually still expose an OpenAI-
compatible `/v1/chat/completions` endpoint — a base URL + API key is enough,
no provider-specific code. Both harnesses take `--api-base` and `--api-key`
generically:

```bash
# mini-swe-agent — passed straight to litellm as api_base/api_key kwargs
uv run task run <bundle> --harness mini-swe-agent \
  --solver openai/glm-4.6 \
  --api-base https://open.bigmodel.cn/api/paas/v4 \
  --api-key <your-glm-key>

# opencode — writes a per-run opencode.json registering <provider-id> as a
# custom @ai-sdk/openai-compatible provider pointing at --api-base;
# <provider-id> is arbitrary, <model-name> is whatever the provider calls it
uv run task run <bundle> --harness opencode \
  --solver kimi/kimi-k2 \
  --api-base https://api.moonshot.ai/v1 \
  --api-key <your-moonshot-key>
```

`--api-key` is never logged, never written to the DB, and for `opencode`
never written to disk inside the container — the generated `opencode.json`
only contains `{env:CUSTOM_API_KEY}`, a reference; the actual value is
forwarded at exec time only, same guarantee as the named providers above.

## Per-bundle harness defaults

Typing `--harness`/`--solver` on every run gets old fast, especially once
you've settled on what a given bundle needs. `task.json` can declare
defaults:

```jsonc
"harness": {
  "harness": "opencode",
  "model": "opencode/north-mini-code-free",
  "api_base": null            // optional, for a custom OpenAI-compatible endpoint
}
```

Precedence: **CLI flag > this block > hardcoded fallback (`stub`)**. So
`uv run task run <bundle>` with no flags at all now runs whatever the bundle
declares, but `--harness mini-swe-agent` on the command line still overrides
it for a one-off comparison. `--api-key` is deliberately *not* part of this
schema and can't be put in `task.json` — that file is meant to be committed
and shared, a key never belongs in it, so it stays CLI/env-only regardless
of what the bundle declares.

Worth being deliberate about what goes here: the solver under test is usually
orthogonal to the task itself (the whole point of a bundle is to benchmark
*many* different LLMs against the same problem), so treat this as "what to
run if I don't say otherwise," not "the one true solver for this task."

## Analysing a run

Everything about a run is already in the DB (transcript, patch, before/after
test results) — `task analyse` turns that into a synthesized, human-readable
report instead of you reading raw JSON:

```bash
export OPENAI_API_KEY=sk-...
uv run task analyse <run-id> --llm openai/gpt-4o
```

Prints a markdown report (`# Test Report` → `## Before Run` → `## After Run`
→ `## What the Model Did` → `## Assessment`), rendered via `rich`. The
"before" section comes from the most recent `task validate` for the same
`task_id` run prior to this one — a heuristic (no explicit foreign key ties
a `run` to the `validate` that preceded it), good enough since a bundle is
normally validated once before any `run`s against it.

Only works on `run` ids (not `init`/`validate`/`analyse` itself — nothing to
analyse there). The `--llm` here is deliberately independent from the
`--harness`/`--solver` that produced the run: use a strong model to judge a
weak one, or vice versa. Same `--api-base`/`--api-key` convention as
`task run`; `--api-key` is never logged.
