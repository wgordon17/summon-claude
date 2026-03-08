"""Interactive terminal selection helpers with TTY-aware fallback."""

# pyright: reportArgumentType=false, reportIndexIssue=false
# pick's type stubs use PICK_RETURN_T generic that doesn't resolve for str options

from __future__ import annotations

import pathlib
import sys
import time

import click


def is_interactive(ctx: click.Context) -> bool:
    """Check if interactive prompts should be used."""
    return sys.stdin.isatty() and not (ctx.obj or {}).get("no_interactive", False)


_MAX_SESSION_NAME = 30


def format_session_option(session: dict, *, annotation: str | None = None) -> str:
    """Format a session dict as a human-readable option string.

    If *annotation* is provided, it replaces the status badge.
    """
    sid = session.get("session_id", "????????")[:8]
    name = session.get("session_name") or "-"
    if len(name) > _MAX_SESSION_NAME:
        name = name[: _MAX_SESSION_NAME - 1] + "\u2026"
    badge = annotation or session.get("status", "?")
    return f"{sid}  {name}  [{badge}]"


def format_log_option(path: pathlib.Path | str) -> str:
    """Format a log file path as a human-readable option string."""
    if not isinstance(path, pathlib.Path):
        path = pathlib.Path(path)

    if path.name == "daemon.log":
        return "daemon    (daemon log)"

    stem = path.stem[:8]
    try:
        age_seconds = int(time.time() - path.stat().st_mtime)
        if age_seconds < 3600:
            age_str = f"{age_seconds // 60}m ago"
        elif age_seconds < 86400:
            age_str = f"{age_seconds // 3600}h ago"
        else:
            age_str = f"{age_seconds // 86400}d ago"
    except OSError:
        age_str = "unknown"
    return f"{stem}  (modified {age_str})"


def interactive_select(
    options: list[str], title: str, ctx: click.Context
) -> tuple[str, int] | None:
    """Present an interactive selection menu. Returns (selected_option, index) or None."""
    if not options:
        return None

    if is_interactive(ctx):
        import pick  # noqa: PLC0415

        try:
            result = pick.pick(options, title, indicator=">")
        except KeyboardInterrupt:
            return None
        return (str(result[0]), int(result[1]))

    # Non-interactive fallback: numbered list with click.prompt
    click.echo(title)
    for i, opt in enumerate(options, 1):
        click.echo(f"  {i}) {opt}")
    choice = click.prompt("Select", type=click.IntRange(1, len(options)))
    return (options[choice - 1], choice - 1)


def interactive_multi_select(
    options: list[str], title: str, ctx: click.Context
) -> list[tuple[str, int]]:
    """Present an interactive multi-selection menu. Returns list of (option, index) tuples."""
    if not options:
        return []

    if is_interactive(ctx):
        import pick  # noqa: PLC0415

        selected = pick.pick(options, title, multiselect=True, min_selection_count=1, indicator=">")
        return [(str(s[0]), int(s[1])) for s in selected]

    # Non-interactive fallback: numbered list with comma-separated input
    click.echo(title)
    for i, opt in enumerate(options, 1):
        click.echo(f"  {i}) {opt}")
    raw = click.prompt("Select (e.g. 1,2,3)")
    seen: set[int] = set()
    result = []
    for raw_token in raw.split(","):
        token = raw_token.strip()
        try:
            idx = int(token)
            if 1 <= idx <= len(options):
                if idx - 1 not in seen:
                    seen.add(idx - 1)
                    result.append((options[idx - 1], idx - 1))
            else:
                click.echo(f"  Skipping out-of-range: {token}", err=True)
        except ValueError:
            click.echo(f"  Skipping invalid: {token}", err=True)
    return result
