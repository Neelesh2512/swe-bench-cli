"""Rich-based console output: colored diffs, syntax-highlighted JSON, tables."""

from __future__ import annotations

import re

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

console = Console()

_TOOL_MARKER_STYLE = {
    "→": "cyan",     # read
    "✱": "yellow",   # grep/glob
    "←": "magenta",  # edit
    "✗": "bold red",  # failed action
    ">": "bold blue",  # session header
}


def _diff_line_style(line: str) -> str | None:
    if line.startswith(("+++", "---")):
        return "bold"
    if line.startswith("+"):
        return "green"
    if line.startswith("-"):
        return "red"
    if line.startswith("@@"):
        return "cyan"
    if line.startswith(("diff --git", "Index:", "===")):
        return "bold yellow"
    return None


def render_diff(diff_text: str) -> Text:
    """Line-colored unified diff: +green -red @@cyan headers-bold-yellow."""
    out = Text()
    for line in diff_text.splitlines(keepends=True):
        style = _diff_line_style(line)
        out.append(line, style=style)
    return out


def render_transcript(text: str) -> Text:
    """Solver transcript: diff hunks get diff coloring, tool-call marker
    lines (→ Read, ✱ Grep, ← Edit, ✗ failed) get their own color."""
    out = Text()
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        diff_style = _diff_line_style(stripped)
        if diff_style:
            out.append(line, style=diff_style)
            continue
        marker_style = next((s for m, s in _TOOL_MARKER_STYLE.items() if stripped.startswith(m)), None)
        out.append(line, style=marker_style)
    return out


def print_diff(diff_text: str) -> None:
    console.print(render_diff(diff_text), soft_wrap=True)


def print_transcript(text: str) -> None:
    console.print(render_transcript(text), soft_wrap=True)


def print_json(obj) -> None:
    console.print_json(data=obj)


def print_status_line(ok: bool, text: str) -> None:
    console.print(text, style="bold green" if ok else "bold red", soft_wrap=True)


def _status_style(status: str) -> str:
    return "green" if status == "success" else ("red" if status in ("failed", "error") else "yellow")


def print_command_summary(cmd, results) -> None:
    style = _status_style(cmd["status"])
    console.print(f"[bold]\\[{cmd['id']}][/bold] {escape(str(cmd['command']))} "
                  f"{escape(str(cmd['task_id']))} — [{style}]{escape(cmd['status'])}[/{style}]",
                  soft_wrap=True)
    if cmd["summary"]:
        console.print(f"  {escape(cmd['summary'])}", soft_wrap=True)
    for r in results:
        passed = bool(r["passed_expectation"])
        mark = "[green]OK[/green]" if passed else "[bold red]XX[/bold red]"
        console.print(f"  {mark} {escape(r['bucket']):<10} {escape(r['outcome']):<7} "
                       f"(exp {escape(str(r['expected']))})  {escape(r['test_id'])}", soft_wrap=True)
    if cmd["transcript"]:
        console.print(f"  [dim](run `task logs {cmd['id']} --transcript` for the full solver transcript)[/dim]",
                      soft_wrap=True)


def print_commands_table(rows) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("id", justify="right")
    table.add_column("command")
    table.add_column("status")
    table.add_column("task")
    table.add_column("started_at")
    for r in rows:
        style = _status_style(r["status"])
        table.add_row(str(r["id"]), escape(r["command"]), f"[{style}]{escape(r['status'])}[/{style}]",
                      escape(r["task_id"] or ""), escape(r["started_at"]))
    console.print(table)


def print_bundles_table(bundles, errors) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("path", no_wrap=True)
    table.add_column("task_id", no_wrap=True)
    table.add_column("repo", no_wrap=True)
    table.add_column("fail2pass", justify="right")
    table.add_column("pass2pass", justify="right")
    table.add_column("harness default")
    for b in bundles:
        harness_default = b.harness_defaults.harness or "[dim]-[/dim]"
        repo = re.sub(r"^https://github\.com/", "", b.repo)
        table.add_row(escape(str(b.root)), escape(b.task_id), escape(repo),
                      str(len(b.fail2pass)), str(len(b.pass2pass)), harness_default)
    console.print(table)
    for path, err in errors:
        console.print(f"[bold red]skipped[/bold red] {escape(str(path))}: {escape(str(err))}")
