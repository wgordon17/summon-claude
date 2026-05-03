"""Project subcommand logic for CLI."""

from __future__ import annotations

import json
import logging
import pathlib
import re
from typing import Any

import click

from summon_claude.cli import daemon_client
from summon_claude.cli.helpers import print_local_daemon_hint
from summon_claude.daemon import is_daemon_running
from summon_claude.sessions.hook_types import INCLUDE_GLOBAL_TOKEN
from summon_claude.sessions.hooks import run_lifecycle_hooks
from summon_claude.sessions.registry import SessionRegistry

logger = logging.getLogger(__name__)


async def _run_project_hooks(hook_type: str, *, project_ids: list[str] | None = None) -> None:
    """Run lifecycle hooks for registered projects. Failures are logged, not fatal.

    If *project_ids* is given, only run hooks for those projects.
    Otherwise run for all registered projects.
    """
    try:
        async with SessionRegistry() as registry:
            projects = await registry.list_projects()
            if project_ids is not None:
                id_set = set(project_ids)
                projects = [p for p in projects if p["project_id"] in id_set]
            for project in projects:
                project_id = project["project_id"]
                directory = project.get("directory", "")
                hooks = await registry.get_lifecycle_hooks(hook_type, project_id=project_id)
                if hooks:
                    cwd = pathlib.Path(directory) if directory else pathlib.Path.cwd()
                    errors = await run_lifecycle_hooks(hook_type, cwd, hooks, project_root=cwd)
                    for err in errors:
                        logger.warning(
                            "%s hook error for project %s: %s", hook_type, project["name"], err
                        )
    except Exception as exc:
        logger.warning("Failed to run %s hooks: %s", hook_type, exc)


def _resolve_directory(directory: str) -> str:
    """Resolve and validate a directory path. Raises ClickException if invalid."""
    resolved = pathlib.Path(directory).resolve()
    if not resolved.is_dir():
        raise click.ClickException(f"Directory does not exist: {resolved}")
    return str(resolved)


async def async_project_add(name: str, directory: str, *, jira_jql: str | None = None) -> str:
    """Register a new project and return the project_id.

    When *jira_jql* is provided, the JQL filter is stored immediately after
    project creation.
    """
    resolved = _resolve_directory(directory)
    project_id: str = ""
    async with SessionRegistry() as registry:
        try:
            project_id = await registry.add_project(name, resolved)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        if jira_jql is not None:
            await registry.update_project(project_id, jira_jql=jira_jql or None)
    return project_id


async def async_project_remove(name_or_id: str) -> None:
    """Remove a project by name or ID.

    If the project has active sessions, stops them via daemon IPC first.
    """
    active_ids: list[str] = []
    async with SessionRegistry() as registry:
        try:
            active_ids = await registry.remove_project(name_or_id)
        except ValueError as e:
            raise click.ClickException(str(e)) from e

    # Auto-stop any active sessions that were linked to this project
    if active_ids and is_daemon_running():
        for sid in active_ids:
            try:
                await daemon_client.stop_session(sid)
                click.echo(f"  Stopped session {sid[:8]}...")
            except Exception as e:
                click.echo(f"  Failed to stop session {sid[:8]}...: {e}", err=True)


async def async_project_list() -> list[dict[str, Any]]:
    """Return all projects from the registry."""
    result: list[dict[str, Any]] = []
    async with SessionRegistry() as registry:
        result = await registry.list_projects()
    return result  # noqa: RET504 — pyright requires pre-init before async with


async def async_project_update(name_or_id: str, **kwargs: Any) -> dict | None:
    """Update mutable project fields by name or ID.

    Pass ``jira_jql=""`` to clear the JQL filter.
    Returns effective auto-mode rules dict if auto-mode options were set, else None.
    Raises ``click.ClickException`` if the project is not found.
    """
    auto_deny = kwargs.pop("auto_deny", None)
    auto_allow = kwargs.pop("auto_allow", None)
    auto_environment = kwargs.pop("auto_environment", None)

    async with SessionRegistry() as registry:
        project = await registry.get_project(name_or_id)
        if project is None:
            raise click.ClickException(f"No project found: {name_or_id!r}")

        # Handle standard fields (jira_jql, etc.) — pass None values through so
        # callers can explicitly clear fields (e.g. jira_jql=None clears to NULL).
        if kwargs:
            try:
                await registry.update_project(project["project_id"], **kwargs)
            except (ValueError, KeyError) as e:
                raise click.ClickException(str(e)) from e

        # Handle auto-mode rules (read-merge-write)
        if auto_deny is not None or auto_allow is not None or auto_environment is not None:
            existing_raw = project.get("auto_mode_rules")
            try:
                parsed = json.loads(existing_raw) if existing_raw else {}
            except json.JSONDecodeError:
                parsed = {}
            rules: dict[str, str] = parsed if isinstance(parsed, dict) else {}
            if auto_deny is not None:
                rules["deny"] = auto_deny
            if auto_allow is not None:
                rules["allow"] = auto_allow
            if auto_environment is not None:
                rules["environment"] = auto_environment
            # Store as NULL if all values are empty (fall back to global)
            if all(not v for v in rules.values()):
                await registry.update_project(project["project_id"], auto_mode_rules=None)
            else:
                await registry.update_project(
                    project["project_id"], auto_mode_rules=json.dumps(rules)
                )
            return rules
    return None


async def launch_project_managers() -> None:
    """Start PM sessions for all registered projects that don't have one running.

    Sends a ``project_up`` IPC to the daemon, which handles all
    orchestration: auth, project discovery, and PM session creation.
    The daemon works in the background — results visible via ``project list``.
    """
    response = await daemon_client.project_up(cwd=str(pathlib.Path.cwd()))

    if response.get("type") == "project_up_complete":
        click.echo("All projects already have PM agents running.")
        # Run project_up hooks for all projects even if PMs are already running.
        await _run_project_hooks("project_up")
        return

    if response.get("type") != "project_up_auth_required":
        raise click.ClickException(
            f"Unexpected daemon response: {response.get('message', response.get('type'))}"
        )

    short_code = response["short_code"]
    project_count = response.get("project_count", 0)

    click.echo(f"Starting PM agents for {project_count} project(s)...")
    click.echo(f"\nAuthenticate in Slack: /summon {short_code}")
    click.echo("\nPM sessions will start after authentication.")
    click.echo("Run 'summon project list' to check status.")
    # Hooks run immediately — before auth completes / PMs start.
    # This is intentional: project_up hooks set up the environment
    # (e.g. build deps, start services) that PMs will need once they launch.
    await _run_project_hooks("project_up")


async def stop_project_managers(*, name: str | None = None) -> list[str]:  # noqa: PLR0912, PLR0915
    """Stop active project sessions (PM + children) for registered projects.

    If *name* is given, only stop sessions for that project.
    Otherwise stop all registered projects.

    All sessions (PM and children) are marked ``suspended`` so
    ``project up`` can resume them with full transcript continuity.
    Channel zzz-rename is handled by each session's ``_shutdown()`` path.

    Returns a list of suspended session_ids.
    """
    if not is_daemon_running():
        click.echo("Daemon is not running. No PM sessions to stop.")
        print_local_daemon_hint()
        if name:
            # Filter by name even without daemon — look up project IDs from DB.
            matched_ids: list[str] = []
            async with SessionRegistry() as registry:
                all_projects = await registry.list_projects()
                matched_ids = [p["project_id"] for p in all_projects if p["name"] == name]
            if not matched_ids:
                raise click.ClickException(f"No project named {name!r}.")
            await _run_project_hooks("project_down", project_ids=matched_ids)
        else:
            await _run_project_hooks("project_down")
        return []

    pm_count = 0
    scribe_suspended = False
    gpm_suspended = False
    suspended: list[str] = []
    projects: list[dict[str, Any]] = []
    async with SessionRegistry() as registry:
        projects = await registry.list_projects()
        if not projects:
            click.echo("No projects registered.")
            return []

        if name:
            projects = [p for p in projects if p["name"] == name]
            if not projects:
                raise click.ClickException(f"No project named {name!r}.")

        for project in projects:
            pname = project["name"]
            sessions = await registry.get_project_sessions(project["project_id"])
            active = [s for s in sessions if s.get("status") in ("pending_auth", "active")]
            # Stop children before PMs so a PM mid-turn doesn't react to
            # its children disappearing.
            active.sort(key=lambda s: bool(s.get("pm_profile")))
            for session in active:
                sid = session["session_id"]
                sname = session.get("session_name", "")
                is_pm = bool(session.get("pm_profile"))
                try:
                    found = await daemon_client.stop_session(sid)
                    if not found:
                        continue
                    # Mark both PM and child sessions as suspended for cascade restart
                    await registry.update_status(sid, "suspended")
                    suspended.append(sid)
                    if is_pm:
                        pm_count += 1
                        click.echo(f"  Suspended PM for {pname!r} ({sid[:8]}...)")
                    else:
                        label = sname or sid[:8]
                        click.echo(f"  Suspended {label!r} for {pname!r}")
                except Exception as e:
                    click.echo(f"  Failed to stop session {sid[:8]}...: {e}", err=True)

        # Stop global scribe and Global PM if running (no project_id)
        if not name:
            all_active = await registry.list_active()
            for sess in all_active:
                sname = sess.get("session_name", "")
                if sess.get("project_id") is not None:
                    continue
                sid = sess["session_id"]
                if sname == "scribe":
                    try:
                        found = await daemon_client.stop_session(sid)
                        if found:
                            await registry.update_status(sid, "suspended")
                            suspended.append(sid)
                            scribe_suspended = True
                            click.echo(f"  Suspended scribe ({sid[:8]}...)")
                    except Exception as e:
                        click.echo(f"  Failed to stop scribe: {e}", err=True)
                elif sname == "global-pm":
                    try:
                        found = await daemon_client.stop_session(sid)
                        if found:
                            await registry.update_status(sid, "suspended")
                            suspended.append(sid)
                            gpm_suspended = True
                            click.echo(f"  Suspended Global PM ({sid[:8]}...)")
                    except Exception as e:
                        click.echo(f"  Failed to stop Global PM: {e}", err=True)

    if not suspended:
        click.echo("No active project sessions found.")
    else:
        n_child = (
            len(suspended) - pm_count - (1 if scribe_suspended else 0) - (1 if gpm_suspended else 0)
        )
        parts: list[str] = []
        if pm_count:
            parts.append(f"{pm_count} PM{'s' if pm_count != 1 else ''}")
        if n_child:
            parts.append(f"{n_child} subsession{'s' if n_child != 1 else ''}")
        if scribe_suspended:
            parts.append("scribe")
        if gpm_suspended:
            parts.append("Global PM")
        click.echo(f"Suspended {', '.join(parts)}.")

    # Clear any queued sessions for stopped projects (best-effort)
    for project in projects:
        try:
            count = await daemon_client.clear_project_queue(project["project_id"])
            if count:
                click.echo(f"  Cleared {count} queued session(s) for {project['name']!r}.")
        except Exception as e:
            logger.debug("clear_project_queue failed for %s: %s", project["name"], e)

    # Run project_down hooks for all projects in the filter (even those with no
    # active sessions — their services/environment still need teardown).
    target_project_ids = [p["project_id"] for p in projects]
    await _run_project_hooks("project_down", project_ids=target_project_ids)

    return suspended


# ---------------------------------------------------------------------------
# Workflow instruction helpers
# ---------------------------------------------------------------------------


async def _resolve_project(registry: SessionRegistry, name_or_id: str) -> dict[str, Any]:
    """Resolve a project by name or ID prefix. Raises ClickException if not found."""
    projects = await registry.list_projects()
    project = next(
        (p for p in projects if p["name"] == name_or_id or p["project_id"].startswith(name_or_id)),
        None,
    )
    if not project:
        raise click.ClickException(f"Project not found: {name_or_id!r}")
    return project


def _strip_comment_lines(text: str) -> str:
    """Strip lines that are pure comments (# followed by space or end of line)."""
    stripped_lines = [
        line
        for line in text.splitlines()
        if not re.match(r"^#\s", line) and not re.match(r"^#$", line)
    ]
    return "\n".join(stripped_lines).strip()


async def async_workflow_show(project_name: str | None = None, *, raw: bool = False) -> None:  # noqa: PLR0912
    """Show workflow instructions with source label."""
    async with SessionRegistry() as registry:
        if project_name:
            project = await _resolve_project(registry, project_name)
            project_id = project["project_id"]
            project_wf = await registry.get_project_workflow(project_id)
            global_wf = await registry.get_workflow_defaults()

            if project_wf is None:
                # Falls back to global
                if global_wf:
                    click.echo(f"Workflow for '{project['name']}' (using global defaults):")
                    click.echo(global_wf)
                else:
                    click.echo("No workflow instructions configured.")
            elif not project_wf:
                # Explicitly cleared (empty string)
                click.echo(
                    f"Workflow for '{project['name']}' (explicitly cleared — no instructions):"
                )
            else:
                has_token = INCLUDE_GLOBAL_TOKEN in project_wf
                if raw:
                    label = f"Workflow for '{project['name']}' (project-specific, raw):"
                    click.echo(label)
                    click.echo(project_wf)
                else:
                    if has_token:
                        label = (
                            f"Workflow for '{project['name']}'"
                            f" (project-specific, includes global via {INCLUDE_GLOBAL_TOKEN}):"
                        )
                    else:
                        label = f"Workflow for '{project['name']}' (project-specific):"
                    click.echo(label)
                    effective = project_wf.replace(INCLUDE_GLOBAL_TOKEN, global_wf)
                    click.echo(effective)
        else:
            global_wf = await registry.get_workflow_defaults()
            if global_wf:
                click.echo("Global workflow defaults:")
                click.echo(global_wf)
            else:
                click.echo("No workflow instructions configured.")


async def async_workflow_set(project_name: str | None = None) -> None:
    """Set workflow instructions via $EDITOR."""
    async with SessionRegistry() as registry:
        if project_name:
            await _workflow_set_project(registry, project_name)
        else:
            await _workflow_set_global(registry)


async def _workflow_set_global(registry: SessionRegistry) -> None:
    """Set global workflow defaults via $EDITOR."""
    current = await registry.get_workflow_defaults()
    template = (
        "# Global workflow defaults — applied to all projects without overrides.\n"
        "# Lines starting with # are stripped on save.\n\n"
    )
    prefill = template + (current or "")

    edited = click.edit(text=prefill)
    if edited is None:
        click.echo("No changes made.")
        return

    content = _strip_comment_lines(edited)
    if not content:
        click.echo("No content entered — no changes made.")
        return

    await registry.set_workflow_defaults(content)
    click.echo("Global workflow defaults updated.")


async def _workflow_set_project(registry: SessionRegistry, project_name: str) -> None:
    """Set per-project workflow instructions via $EDITOR."""
    project = await _resolve_project(registry, project_name)
    pid = project["project_id"]
    pname = project["name"]
    current = await registry.get_project_workflow(pid)
    global_wf = await registry.get_workflow_defaults()

    lines = [
        f"# Workflow instructions for project '{pname}'",
        "# Lines starting with # are stripped on save.",
        "#",
    ]
    if global_wf:
        glines = global_wf.splitlines()
        lines.append("# Current global defaults (for reference):")
        lines.append("# " + "\u2500" * 40)
        for gline in glines[:5]:
            lines.append(f"# {gline}")
        if len(glines) > 5:
            lines.append("# ...")
        lines.append("#")
    lines.append(f"# Use {INCLUDE_GLOBAL_TOKEN} anywhere to include the global defaults inline.")
    lines.append(
        f"# Without {INCLUDE_GLOBAL_TOKEN}, these instructions fully replace the global defaults."
    )
    lines.append("")
    template = "\n".join(lines)
    prefill = template + (current or "")

    edited = click.edit(text=prefill)
    if edited is None:
        click.echo("No changes made.")
        return

    content = _strip_comment_lines(edited)
    if not content:
        click.echo("No content entered — no changes made.")
        return

    await registry.set_project_workflow(pid, content)
    if INCLUDE_GLOBAL_TOKEN in content:
        click.echo(
            f"Workflow updated for project '{pname}'"
            f" (includes global defaults via {INCLUDE_GLOBAL_TOKEN})."
        )
    else:
        click.echo(f"Workflow updated for project '{pname}'.")


async def async_workflow_clear(project_name: str | None = None) -> None:
    """Clear workflow instructions (with confirmation)."""
    async with SessionRegistry() as registry:
        if project_name:
            project = await _resolve_project(registry, project_name)
            if not click.confirm(
                f"Clear project-specific workflow for '{project['name']}'?"
                " Global defaults will apply."
            ):
                click.echo("Cancelled.")
                return
            await registry.clear_project_workflow(project["project_id"])
            click.echo(f"Cleared. Project '{project['name']}' now uses global defaults.")
        else:
            if not click.confirm(
                "Clear global workflow defaults?"
                " Projects without overrides will have no instructions."
            ):
                click.echo("Cancelled.")
                return
            await registry.clear_workflow_defaults()
            click.echo("Global workflow defaults cleared.")
