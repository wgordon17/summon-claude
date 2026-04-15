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

# Column widths for log picker — shared between format_log_option and the header
_LOG_COL_ID = 10
_LOG_COL_STATUS = 12
_LOG_COL_NAME = _MAX_SESSION_NAME + 2
_LOG_COL_CHANNEL = 16
_BACK_LABEL = "← Back"


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


def _format_age(mtime: float) -> str:
    """Format a file's mtime as a human-readable relative age string."""
    age_seconds = int(time.time() - mtime)
    if age_seconds < 3600:
        return f"{age_seconds // 60}m ago"
    if age_seconds < 86400:
        return f"{age_seconds // 3600}h ago"
    return f"{age_seconds // 86400}d ago"


def format_log_option(
    path: pathlib.Path | str,
    session_meta: dict | None = None,
) -> str:
    """Format a log file path as a human-readable option string.

    When *session_meta* is provided (a registry row dict), the output
    mirrors ``session list`` style::

        a1b2c3d4  completed  my-session          #summon-abc  2h ago

    Without metadata, falls back to a simpler format.
    """
    if not isinstance(path, pathlib.Path):
        path = pathlib.Path(path)

    try:
        age_str = _format_age(path.stat().st_mtime)
    except OSError:
        age_str = "unknown"

    if path.name == "daemon.log":
        return (
            f"{'daemon':<{_LOG_COL_ID}}{'-':<{_LOG_COL_STATUS}}"
            f"{'daemon log':<{_LOG_COL_NAME}}{'-':<{_LOG_COL_CHANNEL}}"
            f"{age_str}"
        )

    short_id = path.stem[:8]

    if session_meta:
        status = session_meta.get("status", "?")
        name = session_meta.get("session_name") or "-"
        if len(name) > _MAX_SESSION_NAME:
            name = name[: _MAX_SESSION_NAME - 1] + "\u2026"
        channel = session_meta.get("slack_channel_name") or "-"
        return (
            f"{short_id:<{_LOG_COL_ID}}{status:<{_LOG_COL_STATUS}}"
            f"{name:<{_LOG_COL_NAME}}{channel:<{_LOG_COL_CHANNEL}}"
            f"{age_str}"
        )

    return f"{short_id}  (modified {age_str})"


LOG_PICKER_HEADER = (
    f"{'ID':<{_LOG_COL_ID}}{'STATUS':<{_LOG_COL_STATUS}}"
    f"{'NAME':<{_LOG_COL_NAME}}{'CHANNEL':<{_LOG_COL_CHANNEL}}AGE"
)


def interactive_select(
    options: list[str],
    title: str,
    ctx: click.Context,
    *,
    default_index: int = 0,
    catch_interrupt: bool = True,
    back_label: bool = True,
    hint: str = "(ctrl+c to exit)",
) -> tuple[str, int] | None:
    """Present an interactive selection menu. Returns (selected_option, index) or None.

    Args:
        default_index: Pre-selected option index (clamped to valid range).
        catch_interrupt: When True, catches KeyboardInterrupt and returns None.
            When False, lets KeyboardInterrupt propagate to the caller.
        back_label: When True, appends a "← Back" option in interactive mode.
        hint: Text appended to the title's first line in interactive mode.
    """
    if not options:
        return None

    default_index = max(0, min(default_index, len(options) - 1))

    if is_interactive(ctx):
        import pick  # noqa: PLC0415

        picker_options = [*options, _BACK_LABEL] if back_label else options
        # Append hint to first line only (title may contain \n for subheaders)
        lines = title.split("\n", 1)
        lines[0] += f"  {hint}"
        picker_title = "\n".join(lines)
        try:
            result = pick.pick(
                picker_options, picker_title, indicator=">", default_index=default_index
            )
        except KeyboardInterrupt:
            if catch_interrupt:
                return None
            raise
        idx = int(result[1])
        if back_label and idx >= len(options):
            return None
        return (str(result[0]), idx)

    # Non-interactive fallback: numbered list with click.prompt
    click.echo(title)
    for i, opt in enumerate(options, 1):
        marker = "*" if i - 1 == default_index else " "
        click.echo(f"  {marker} {i}) {opt}")
    try:
        choice = click.prompt(
            "Select", type=click.IntRange(1, len(options)), default=default_index + 1
        )
    except (KeyboardInterrupt, click.Abort):
        if catch_interrupt:
            return None
        raise
    return (options[choice - 1], choice - 1)


def _init_select(options: list[str], title: str, ctx: click.Context, default_index: int = 0) -> str:
    """Init wizard selector — wraps interactive_select with propagating interrupts."""
    result = interactive_select(
        options,
        title,
        ctx,
        default_index=default_index,
        catch_interrupt=False,
        back_label=False,
        hint="(↑/↓ to select, Enter to confirm)",
    )
    return result[0] if result else ""


def interactive_multi_select(
    options: list[str], title: str, ctx: click.Context
) -> list[tuple[str, int]]:
    """Present an interactive multi-selection menu. Returns list of (option, index) tuples."""
    if not options:
        return []

    if is_interactive(ctx):
        import pick  # noqa: PLC0415

        hint = f"{title}  (ctrl+c to exit)"
        selected = pick.pick(options, hint, multiselect=True, min_selection_count=1, indicator=">")
        return [(str(s[0]), int(s[1])) for s in selected]

    # Non-interactive fallback: numbered list with comma-separated input
    click.echo(title)
    for i, opt in enumerate(options, 1):
        click.echo(f"  {i}) {opt}")
    try:
        raw = click.prompt("Select (e.g. 1,2,3)")
    except (KeyboardInterrupt, click.Abort):
        return []
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
