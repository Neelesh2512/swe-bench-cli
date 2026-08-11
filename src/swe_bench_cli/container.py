"""Docker helpers: image pull, throwaway containers, patch apply, test exec."""

from __future__ import annotations

import io
import re
import tarfile

import docker

_DIFF_GIT_FILE_RE = re.compile(r"^diff --git a/(\S+) b/\S+", re.MULTILINE)


def client() -> "docker.DockerClient":
    return docker.from_env()


def ensure_image(cli, image: str) -> str:
    """Pull image if absent. Return its resolved id (digest-ish) for the log."""
    try:
        img = cli.images.get(image)
    except docker.errors.ImageNotFound:
        img = cli.images.pull(image)
    return img.id


def start(cli, image: str, workdir: str, network_disabled: bool = True):
    """Start a throwaway detached container that idles, so we can exec into it.

    ponytail: network off + no host mounts by default = solver isolation.
    Loosen per-bundle later if a task legitimately needs network.
    """
    return cli.containers.run(
        image,
        command="sleep infinity",
        working_dir=workdir,
        detach=True,
        network_disabled=network_disabled,
        auto_remove=False,   # we commit/remove explicitly
    )


def build_image(cli, base_image: str, build_cmd: str, tag: str) -> str:
    """Build the task image: run build_cmd (clone+deps) in a throwaway
    container from base_image, then commit it to `tag`. Network stays on
    for this step only (clone/pip install need it); every later container
    started from `tag` runs with network disabled.
    """
    ensure_image(cli, base_image)
    cont = cli.containers.run(
        base_image, command="sleep infinity", working_dir="/",
        detach=True, network_disabled=False, auto_remove=False,
    )
    try:
        rc, out = exec_run(cont, build_cmd, workdir="/")
        if rc != 0:
            raise RuntimeError(f"build_cmd failed (rc={rc}):\n{out}")
        repo, _, t = tag.partition(":")
        cont.commit(repository=repo, tag=t or "latest")
    finally:
        cont.remove(force=True)
    return cli.images.get(tag).id


def exec_run(container, cmd: str, workdir: str | None = None, tty: bool = False,
              env: dict[str, str] | None = None):
    """Run a shell command; return (exit_code, combined_output_str).

    tty=True allocates a pseudo-terminal for the exec session. Some CLIs
    (observed: opencode) block on terminal-dependent I/O (their UI renderer)
    when run headless without one -- confirmed by reproducing a hang without
    tty and a clean run with it, same command otherwise.

    env is scoped to this exec call only -- unlike the container's own
    Config.Env, it is NOT captured by `container.commit()`, so provider API
    keys passed here never leak into a run-snapshot image.
    """
    kwargs = {"cmd": ["sh", "-c", cmd], "tty": tty}
    if env:
        kwargs["environment"] = env
    if workdir:
        kwargs["workdir"] = workdir
    res = container.exec_run(**kwargs)
    return res.exit_code, res.output.decode("utf-8", "replace")


def put_file(container, path: str, content: str) -> None:
    """Write a text file into the container at `path` via tar put_archive."""
    data = content.encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        info = tarfile.TarInfo(name=path.lstrip("/").split("/")[-1])
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    stream.seek(0)
    dest_dir = "/".join(path.split("/")[:-1]) or "/"
    container.put_archive(dest_dir, stream.read())


def reset_files_touched_by(container, workdir: str, patch_text: str) -> None:
    """Discard any local changes to files a patch is about to touch.

    Used before staging test_patch: gated test files aren't part of the
    graded solution, they're about to be overwritten by test_patch anyway.
    A harness that edited/added its own version of one of them (e.g. writing
    its own regression tests while validating a fix -- observed for real)
    would otherwise make `git apply` fail on a context mismatch. Discarding
    only these specific paths leaves the harness's actual source-code fix
    (in other files) untouched.
    """
    paths = _DIFF_GIT_FILE_RE.findall(patch_text)
    if not paths:
        return
    # ignore individual failures (e.g. a brand-new path test_patch adds that
    # doesn't exist yet at HEAD -- nothing to discard, that's fine)
    exec_run(container, "git checkout HEAD -- " + " ".join(paths) + " 2>/dev/null; true",
             workdir=workdir)


def apply_patch(container, workdir: str, patch_text: str) -> None:
    """git apply a unified diff inside the container."""
    put_file(container, f"{workdir}/.__patch.diff", patch_text)
    rc, out = exec_run(
        container,
        "git apply --whitespace=nowarn .__patch.diff && rm -f .__patch.diff",
        workdir=workdir,
    )
    if rc != 0:
        raise RuntimeError(f"git apply failed (rc={rc}):\n{out}")


def capture_diff(container, workdir: str) -> str:
    """git diff of current repo state — the solver's produced patch."""
    _, out = exec_run(container, "git add -A && git diff --cached", workdir=workdir)
    return out


def snapshot(container, tag: str) -> None:
    """Commit container state to an image tag for post-run debugging."""
    container.commit(repository=tag.split(":")[0], tag=tag.split(":")[-1])
