"""Bundle loading + task.json schema."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

PREBUILT_REGISTRY = "jefzda/sweap-images"


@dataclass
class Env:
    test_cmd: str = "python -m pytest -q {test_id}"
    workdir: str = "/app"
    base_image: str | None = None
    build_cmd: str | None = None


@dataclass
class HarnessDefaults:
    """Optional per-bundle convenience defaults for `task run`.

    `harness` = the driver/framework (opencode, mini-swe-agent, stub).
    `model` = the actual solver -- the LLM being benchmarked -- since that's
    the thing this whole tool exists to measure.

    Deliberately excludes api_key -- task.json is meant to be committed and
    shared, a key never belongs in it. CLI flags always override these; these
    only override the CLI's own hardcoded fallback. The solver under test is
    normally orthogonal to the task (same bundle should run against many
    different LLMs to benchmark them), so this is a default, not a pin.
    """
    harness: str | None = None
    model: str | None = None
    api_base: str | None = None


@dataclass
class Bundle:
    root: Path
    task_id: str
    repo: str
    base_commit: str
    dockerhub_tag: str | None
    env: Env
    test_patch: str          # filename, relative to root
    golden_patch: str        # filename, relative to root
    fail2pass: list[str]
    pass2pass: list[str]
    timeout_sec: int = 1800
    harness_defaults: HarnessDefaults = field(default_factory=HarnessDefaults)

    @property
    def image(self) -> str:
        """Resolved image ref.

        base_image set -> `init` builds+tags a local image at `built_image_tag`,
        used by every later command. base_image absent -> pull the prebuilt
        SWE-bench Pro image from dockerhub_tag directly.
        """
        if self.env.base_image:
            return self.built_image_tag
        if self.dockerhub_tag:
            return f"{PREBUILT_REGISTRY}:{self.dockerhub_tag}"
        raise ValueError(f"{self.task_id}: no base_image and no dockerhub_tag")

    @property
    def built_image_tag(self) -> str:
        safe = self.task_id.replace("/", "_")
        return f"task-{safe}:base"

    def test_patch_text(self) -> str:
        return (self.root / self.test_patch).read_text()

    def golden_patch_text(self) -> str:
        return (self.root / self.golden_patch).read_text()

    @classmethod
    def load(cls, path: str | Path) -> "Bundle":
        """Load a bundle directory (containing task.json)."""
        root = Path(path)
        raw = json.loads((root / "task.json").read_text())
        env = Env(**raw.get("env", {}))
        return cls(
            root=root,
            task_id=raw["task_id"],
            repo=raw["repo"],
            base_commit=raw["base_commit"],
            dockerhub_tag=raw.get("dockerhub_tag"),
            env=env,
            test_patch=raw["test_patch"],
            golden_patch=raw["golden_patch"],
            fail2pass=raw["fail2pass"],
            pass2pass=raw["pass2pass"],
            timeout_sec=raw.get("timeout_sec", 1800),
            harness_defaults=HarnessDefaults(**raw.get("harness", {})),
        )


def discover(root: str | Path) -> tuple[list["Bundle"], list[tuple[Path, Exception]]]:
    """Find bundles under `root`: any immediate subdirectory with a task.json.

    Returns (loaded, errors) rather than raising, so one malformed bundle
    doesn't hide every other one -- the caller decides how to report errors.
    """
    root = Path(root)
    loaded, errors = [], []
    if not root.is_dir():
        return loaded, errors
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (child / "task.json").is_file():
            continue
        try:
            loaded.append(Bundle.load(child))
        except Exception as e:
            errors.append((child, e))
    return loaded, errors
