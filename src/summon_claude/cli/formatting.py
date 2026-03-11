"""Formatting helpers for CLI output."""

from __future__ import annotations

import json
from datetime import datetime

import click


def echo(msg: str, ctx: click.Context, err: bool = False) -> None:
    if err or not ctx.obj.get("quiet"):
        click.echo(msg, err=err)


def format_json(data: list[dict] | dict) -> str:
    return json.dumps(data, indent=2, default=str)


def format_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts


def format_uptime(seconds: float) -> str:
    """Format a duration in seconds as a human-readable uptime string."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def print_session_table(sessions: list[dict]) -> None:
    """Print a compact table of sessions."""
    if not sessions:
        return

    headers = ["ID", "STATUS", "NAME", "CHANNEL", "CWD"]
    rows: list[list[str]] = []
    for s in sessions:
        session_id = s.get("session_id", "")
        # Show first 8 chars of UUID — enough to be unique and passable to `stop`
        short_id = session_id[:8] if session_id else "-"
        rows.append(
            [
                short_id,
                s.get("status", "?"),
                s.get("session_name") or "-",
                s.get("slack_channel_name") or "-",
                s.get("cwd", ""),
            ]
        )

    # Fixed-width for all columns except CWD (last), which wraps freely
    fixed = headers[:-1]
    col_widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(fixed)]
    prefix_fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    click.echo(f"{prefix_fmt.format(*fixed)}  {headers[-1]}")
    click.echo("  ".join("-" * w for w in col_widths) + "  " + "-" * len(headers[-1]))
    for row in rows:
        click.echo(f"{prefix_fmt.format(*row[:-1])}  {row[-1]}")


def print_session_detail(session: dict) -> None:
    """Print detailed info for a single session."""
    fields = [
        ("Session ID", session.get("session_id", "")),
        ("Status", session.get("status", "")),
        ("Name", session.get("session_name") or "-"),
        ("PID", str(session.get("pid", ""))),
        ("CWD", session.get("cwd", "")),
        ("Model", session.get("model") or "-"),
        ("Channel ID", session.get("slack_channel_id") or "-"),
        ("Channel", session.get("slack_channel_name") or "-"),
        ("Claude Session", session.get("claude_session_id") or "-"),
        ("Started", format_ts(session.get("started_at"))),
        ("Authenticated", format_ts(session.get("authenticated_at"))),
        ("Last Activity", format_ts(session.get("last_activity_at"))),
        ("Ended", format_ts(session.get("ended_at"))),
        ("Turns", str(session.get("total_turns", 0))),
        ("Total Cost", f"${session.get('total_cost_usd', 0.0) or 0.0:.4f}"),
    ]
    if session.get("error_message"):
        fields.append(("Error", session["error_message"]))

    max_key = max(len(k) for k, _ in fields)
    for key, val in fields:
        click.echo(f"  {key.ljust(max_key)} : {val}")
