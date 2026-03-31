"""Google OAuth setup wizard, authentication, and status checks."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from summon_claude.config import get_config_file, get_google_credentials_dir

logger = logging.getLogger(__name__)

_CHOICE_CURRENT = "use-current"
_CHOICE_EXISTING = "enter-existing"
_CHOICE_NEW = "create-new"
_CHOICE_SKIP = "skip"

_SETUP_STEPS = [
    "Google Cloud Project",
    "Enable APIs",
    "OAuth Consent Screen",
    "Create OAuth Client",
]


def _setup_roadmap(step: int, completed: dict[int, str]) -> str:
    """Return the step roadmap as a plain-text string (for pick titles)."""
    lines = [
        f"Google OAuth Setup                            Step {step} of {len(_SETUP_STEPS)}",
        "-" * 60,
    ]
    for i, title in enumerate(_SETUP_STEPS, 1):
        if i in completed:
            detail = f" [{completed[i]}]" if completed[i] else ""
            lines.append(f"  ✓ {i}. {title}{detail}")
        elif i == step:
            lines.append(f"  ◉ {i}. {title}")
        else:
            lines.append(f"    {i}. {title}")
    lines.append("")
    lines.append("-" * 60)
    lines.append("")
    return "\n".join(lines)


def _setup_header(step: int, completed: dict[int, str], *, skip_clear: bool = False) -> None:
    """Render a step header with progress roadmap to the terminal."""
    if not skip_clear:
        click.clear()
    click.secho(
        f"Google OAuth Setup                            Step {step} of {len(_SETUP_STEPS)}",
        bold=True,
    )
    click.echo(click.style("-" * 60, dim=True))
    for i, title in enumerate(_SETUP_STEPS, 1):
        if i in completed:
            detail = f" [{completed[i]}]" if completed[i] else ""
            click.secho(f"  ✓ {i}. {title}{detail}", fg="green")
        elif i == step:
            click.secho(f"  ◉ {i}. {title}", bold=True)
        else:
            click.secho(f"    {i}. {title}", dim=True)
    click.echo()


def _run_gcloud(
    gcloud_bin: str,
    args: list[str],
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a gcloud command and return the result."""
    return subprocess.run(  # noqa: S603
        [gcloud_bin, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _make_console_url_builder(
    gcloud_email: str | None,
):
    """Return a function that builds Google Console URLs.

    With *gcloud_email*: appends ``authuser=<email>`` to the URL.
    Without: wraps through the Google account chooser.
    """
    from urllib.parse import quote, urlencode, urlparse, urlunparse  # noqa: PLC0415

    def _build(base_url: str, **extra_params: str) -> str:
        parsed = urlparse(base_url)
        params: dict[str, str] = {}
        # Preserve existing query params (e.g. ?apiid=...)
        if parsed.query:
            for part in parsed.query.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[k] = v
        params.update(extra_params)
        if gcloud_email:
            params["authuser"] = gcloud_email
        qs = urlencode(params)
        target = urlunparse(parsed._replace(query=qs))
        if gcloud_email:
            return target
        return f"https://accounts.google.com/AccountChooser?continue={quote(target, safe='')}"

    return _build


def _open_or_print(url: str) -> None:
    """Print a URL and offer to open it in the browser."""
    click.secho(f"  {url}", fg="cyan")
    if click.confirm("  Open in browser?", default=True):
        click.launch(url)


def _show_and_open(urls: list[tuple[str, str]]) -> None:
    """Print labelled URLs, then offer to open all at once.

    *urls* is a list of ``(label, url)`` pairs.
    """
    for label, url in urls:
        click.secho(f"  {label}:", bold=True)
        click.secho(f"    {url}", fg="cyan")
    click.echo()
    if click.confirm("  Open all in browser?", default=True):
        for _, url in urls:
            click.launch(url)


def google_setup() -> None:
    """Interactive guided setup for Google OAuth credentials."""
    # Check for existing credentials
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    secrets_file = get_google_credentials_dir() / "client_env"

    if not (client_id and client_secret) and secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            if line.startswith("GOOGLE_OAUTH_CLIENT_ID="):
                client_id = line.split("=", 1)[1].strip()
            elif line.startswith("GOOGLE_OAUTH_CLIENT_SECRET="):
                client_secret = line.split("=", 1)[1].strip()

    if (
        client_id
        and client_secret
        and not click.confirm(
            "Google OAuth credentials already configured. Re-run setup?", default=False
        )
    ):
        return

    project_id: str | None = None
    _gcloud_bin = shutil.which("gcloud")
    has_gcloud = _gcloud_bin is not None
    completed: dict[int, str] = {}

    # Detect gcloud account email for URL authuser parameter
    _gcloud_email: str | None = None
    if has_gcloud:
        try:
            result = _run_gcloud(_gcloud_bin, ["config", "get-value", "account"], timeout=10)
            _val = result.stdout.strip()
            if result.returncode == 0 and _val and _val != "(unset)" and "@" in _val:
                _gcloud_email = _val
        except (subprocess.TimeoutExpired, OSError):
            pass

    _url = _make_console_url_builder(_gcloud_email)

    # ── Step 1: GCP Project ─────────────────────────────────────────────

    # Detect current gcloud project (before rendering, to build choices)
    _current_project: str | None = None
    if has_gcloud:
        try:
            result = _run_gcloud(_gcloud_bin, ["config", "get-value", "project"], timeout=10)
            _val = result.stdout.strip()
            if result.returncode == 0 and _val and _val != "(unset)":
                _current_project = _val
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Build choices
    _choices: list[str] = []
    _choice_keys: list[str] = []
    if _current_project:
        _choices.append(f"Use current gcloud project: {_current_project}")
        _choice_keys.append(_CHOICE_CURRENT)
    _choices.append("Enter an existing project ID")
    _choice_keys.append(_CHOICE_EXISTING)
    _choices.append("Create a new project")
    _choice_keys.append(_CHOICE_NEW)
    _choices.append("Skip this step")
    _choice_keys.append(_CHOICE_SKIP)

    _project_id_re = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")

    while True:  # Loop allows "go back" after resolution
        project_id = None

        if sys.stdin.isatty():
            import pick  # noqa: PLC0415

            _pick_title = _setup_roadmap(1, completed) + "Select or create a Google Cloud project:"
            try:
                _, idx = pick.pick(_choices, _pick_title, indicator=">")
            except KeyboardInterrupt:
                return
            choice = _choice_keys[int(idx)]  # type: ignore[arg-type]
        else:
            _setup_header(1, completed)
            click.echo("Select or create a Google Cloud project.\n")
            for i, label in enumerate(_choices, 1):
                click.echo(f"  {i}) {label}")
            try:
                raw_idx = click.prompt("Select", type=click.IntRange(1, len(_choices)))
            except (KeyboardInterrupt, click.Abort):
                return
            choice = _choice_keys[raw_idx - 1]

        # Re-render after pick exits curses
        _setup_header(1, completed)

        if choice == _CHOICE_CURRENT:
            project_id = _current_project
        elif choice == _CHOICE_EXISTING:
            _browse_url = _url("https://console.cloud.google.com/projectselector2/home")
            click.echo("Browse your GCP projects:\n")
            _open_or_print(_browse_url)
            click.echo()
            project_id = click.prompt(
                "Enter project ID, name, or number", default="", show_default=False
            ).strip()
        elif choice == _CHOICE_NEW:
            import secrets as secrets_mod  # noqa: PLC0415

            suggested = f"summon-claude-{secrets_mod.token_hex(3)[:5]}"
            project_id = click.prompt("Project ID", default=suggested).strip()
        else:
            break  # skip

        # Resolve names/numbers to project ID (for existing project input)
        if project_id and choice != _CHOICE_NEW and not _project_id_re.match(project_id):
            if has_gcloud:
                try:
                    result = _run_gcloud(
                        _gcloud_bin,
                        ["projects", "describe", project_id, "--format=value(projectId)"],
                        timeout=15,
                    )
                    resolved = result.stdout.strip()
                    if result.returncode == 0 and resolved:
                        project_id = resolved
                    else:
                        click.secho(f"  Could not resolve '{project_id}'.", fg="yellow")
                        project_id = None
                except (subprocess.TimeoutExpired, OSError):
                    click.secho("  Could not reach gcloud to resolve project.", fg="yellow")
            else:
                click.secho(
                    f"  '{project_id}' doesn't look like a project ID"
                    " (install gcloud to resolve names/numbers).",
                    fg="yellow",
                )
                project_id = None

        # Validate format for new projects
        if project_id and choice == _CHOICE_NEW and not _project_id_re.match(project_id):
            click.secho(
                "  Invalid project ID: 6-30 chars, lowercase letters/digits/hyphens,",
                fg="yellow",
            )
            click.secho("  starts with a letter, cannot end with a hyphen.", fg="yellow")
            project_id = None

        if not project_id:
            click.pause("  Press Enter to try again...")
            continue

        # Create the project if needed
        if choice == _CHOICE_NEW:
            _create_browser = _url("https://console.cloud.google.com/projectcreate")
            if has_gcloud:
                click.echo(f'\n  gcloud projects create {project_id} --name="summon-claude"\n')
                if click.confirm("  Run this now?", default=True):
                    try:
                        result = _run_gcloud(
                            _gcloud_bin,
                            ["projects", "create", project_id, "--name=summon-claude"],
                            timeout=60,
                        )
                        if result.returncode == 0:
                            click.secho("  ✓ Project created.", fg="green")
                        else:
                            stderr = result.stderr.strip()
                            if "already exists" in stderr.lower():
                                click.echo("  Project already exists — continuing.")
                            else:
                                click.secho(f"  gcloud error: {stderr}", fg="red")
                                click.echo(f"  Create manually: {_create_browser}")
                    except (subprocess.TimeoutExpired, OSError) as e:
                        click.echo(f"  Could not run gcloud: {e}")
                        click.echo(f"  Create manually: {_create_browser}")
                else:
                    _open_or_print(_create_browser)
            else:
                click.echo("\nCreate your project:\n")
                _open_or_print(_create_browser)

        # Verify project exists (all paths — consistent gate before confirm)
        if has_gcloud:
            try:
                result = _run_gcloud(
                    _gcloud_bin,
                    ["projects", "describe", project_id, "--format=value(projectId)"],
                    timeout=15,
                )
                resolved = result.stdout.strip()
                if result.returncode == 0 and resolved:
                    project_id = resolved  # Canonicalize
                else:
                    click.secho(f"\n  Project '{project_id}' not found.", fg="yellow")
                    click.pause("  Press Enter to try again...")
                    continue
            except (subprocess.TimeoutExpired, OSError):
                pass  # Can't verify — proceed with confirm

        # Show result in the step tracker and confirm
        completed[1] = project_id
        _setup_header(1, completed)
        if click.confirm(f"  Confirm project '{project_id}'?", default=True):
            break
        # User said no — clear and loop back
        del completed[1]

    if 1 not in completed:
        completed[1] = project_id or "skipped"

    # ── Step 2: Enable APIs ─────────────────────────────────────────────
    _setup_header(2, completed)
    click.echo("Enable required Google APIs for your project.\n")

    apis = [
        ("Gmail API", "gmail.googleapis.com"),
        ("Calendar API", "calendar-json.googleapis.com"),
        ("Drive API", "drive.googleapis.com"),
    ]
    _required_api_ids = {api_id for _, api_id in apis}

    def _check_apis_enabled() -> set[str]:
        """Return the set of required APIs that are currently enabled."""
        if not has_gcloud or not project_id:
            return set()
        try:
            result = _run_gcloud(
                _gcloud_bin,  # type: ignore[arg-type]
                [
                    "services",
                    "list",
                    "--enabled",
                    f"--project={project_id}",
                    "--format=value(config.name)",
                ],
                timeout=15,
            )
            if result.returncode == 0:
                enabled = {line.strip() for line in result.stdout.splitlines() if line.strip()}
                return _required_api_ids & enabled
        except (subprocess.TimeoutExpired, OSError):
            pass
        return set()

    # Pre-check: skip if all APIs already enabled
    _already_enabled = _check_apis_enabled()
    if _already_enabled == _required_api_ids:
        click.secho("  ✓ All required APIs already enabled.", fg="green")
        completed[2] = ", ".join(label for label, _ in apis)
    else:
        if _already_enabled:
            for label, api_id in apis:
                if api_id in _already_enabled:
                    click.secho(f"  ✓ {label}", fg="green")
                else:
                    click.secho(f"    {label}", dim=True)
            click.echo()

        _api_url_params = {"project": project_id} if project_id else {}
        _api_urls = [
            (
                label,
                _url(
                    f"https://console.cloud.google.com/flows/enableapi?apiid={api_id}",
                    **_api_url_params,
                ),
            )
            for label, api_id in apis
            if api_id not in _already_enabled
        ]
        _apis_ok = False

        if has_gcloud:
            missing_ids = [a for _, a in apis if a not in _already_enabled]
            project_flag = f" --project={project_id}" if project_id else ""
            _gcloud_cmd = f"gcloud services enable {' '.join(missing_ids)}{project_flag}"
            click.echo(f"  {_gcloud_cmd}\n")
            if click.confirm("  Run this now?", default=True):
                try:
                    result = _run_gcloud(
                        _gcloud_bin,
                        [
                            "services",
                            "enable",
                            *missing_ids,
                            *([f"--project={project_id}"] if project_id else []),
                        ],
                        timeout=60,
                    )
                    if result.returncode == 0:
                        click.secho("  ✓ APIs enabled.", fg="green")
                        _apis_ok = True
                    else:
                        click.secho(f"  gcloud error: {result.stderr.strip()}", fg="red")
                        click.echo("  Enable manually via the links below.\n")
                except (subprocess.TimeoutExpired, OSError) as e:
                    click.echo(f"  Could not run gcloud: {e}")
                    click.echo("  Enable manually via the links below.\n")

        if not _apis_ok:
            _show_and_open(_api_urls)
            click.pause("\n  Press Enter when APIs are enabled...")

            # Post-verify
            if has_gcloud and project_id:
                _now_enabled = _check_apis_enabled()
                _still_missing = _required_api_ids - _now_enabled
                if _still_missing:
                    missing_labels = [lbl for lbl, aid in apis if aid in _still_missing]
                    click.secho(
                        f"  Warning: still not enabled: {', '.join(missing_labels)}",
                        fg="yellow",
                    )

        completed[2] = ", ".join(label for label, _ in apis)

    # ── Step 3: OAuth Consent Screen ────────────────────────────────────
    _setup_header(3, completed)
    _proj_params = {"project": project_id} if project_id else {}
    branding_url = _url("https://console.developers.google.com/auth/branding", **_proj_params)
    audience_url = _url("https://console.developers.google.com/auth/audience", **_proj_params)

    if click.confirm("  Already configured the consent screen?", default=False):
        completed[3] = "already configured"
    else:
        _is_gmail = _gcloud_email and _gcloud_email.endswith("@gmail.com")
        _is_workspace = _gcloud_email and not _is_gmail
        click.echo("\nConfigure the OAuth consent screen:\n")
        click.echo("  1. Under Branding: fill in App name (e.g. 'summon-claude'),")
        click.echo("     User support email (your email)")
        if _is_gmail:
            click.echo("  2. Under Audience: select 'External' user type")
            click.secho("     (required for @gmail.com accounts)", dim=True)
            click.echo("  3. Under Publishing status: click 'Publish App' to switch to Production")
            click.secho("     (avoids 7-day token expiry for External apps)", dim=True)
        elif _is_workspace:
            click.echo("  2. Under Audience: select 'Internal' user type")
            click.secho("     (no unverified-app warning, no user cap, no token expiry)", dim=True)
        else:
            click.echo("  2. Under Audience: select 'Internal' if available, otherwise 'External'")
            click.secho(
                "     (Internal = Workspace only, no warnings; External = any account)",
                dim=True,
            )
            click.echo("  3. If you chose External: click 'Publish App' to switch to Production")
            click.secho("     (avoids 7-day token expiry for External apps)", dim=True)
        click.echo()
        _show_and_open([("Branding", branding_url), ("Audience", audience_url)])
        click.echo()
        if _is_gmail:
            click.secho(
                "  Note: You'll see an 'unverified app' warning during login —"
                " normal for personal use.",
                dim=True,
            )
        click.pause("\n  Press Enter when consent screen is configured...")
        completed[3] = "done"

    # ── Step 4: Create OAuth Client ─────────────────────────────────────
    _setup_header(4, completed)
    client_url = _url("https://console.developers.google.com/auth/clients/create", **_proj_params)

    click.echo("Create an OAuth client:\n")
    click.echo("  1. Application type: 'Desktop app'")
    click.echo("  2. Name: 'summon-claude' (or anything)")
    click.echo("  3. Click 'Create'")
    click.echo("  4. Click 'Download JSON' to save client_secret.json\n")
    _open_or_print(client_url)

    # Credential input loop
    import glob as glob_mod  # noqa: PLC0415
    import json as json_mod  # noqa: PLC0415
    import readline  # noqa: PLC0415

    _downloads = Path.home() / "Downloads"

    def _scan_downloads() -> list[Path]:
        if not _downloads.is_dir():
            return []
        return sorted(
            _downloads.glob("client_secret*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def _offer_detected(detected: list[Path]) -> str | None:
        """Offer detected files to the user. Returns selected path or None."""
        if len(detected) == 1:
            click.secho(f"\n  Found: {detected[0].name}", fg="green")
            if click.confirm(f"  Use {detected[0]}?", default=True):
                return str(detected[0])
            return None
        # Multiple files
        click.echo()
        if sys.stdin.isatty():
            import pick  # noqa: PLC0415

            _file_choices = [f"{p.name}  ({p.parent})" for p in detected]
            _file_choices.append("Enter path manually")
            try:
                _, idx = pick.pick(
                    _file_choices,
                    "Found client_secret files in Downloads:",
                    indicator=">",
                )
                if int(idx) < len(detected):  # type: ignore[arg-type]
                    return str(detected[int(idx)])  # type: ignore[arg-type]
            except KeyboardInterrupt:
                pass
        else:
            click.echo("  Found client_secret files in Downloads:\n")
            for i, p in enumerate(detected, 1):
                click.echo(f"    {i}) {p.name}")
            click.echo(f"    {len(detected) + 1}) Enter path manually")
            try:
                raw = click.prompt("  Select", type=click.IntRange(1, len(detected) + 1))
                if raw <= len(detected):
                    return str(detected[raw - 1])
            except (KeyboardInterrupt, click.Abort):
                pass
        return None

    # Set up readline tab-completion for manual path entry
    def _path_completer(text: str, state: int) -> str | None:
        expanded = str(Path(text).expanduser())
        # glob.glob required here — readline expects plain strings, not Path objects
        matches = glob_mod.glob(expanded + "*")  # noqa: PTH207
        matches = [m + "/" if Path(m).is_dir() else m for m in matches]
        return matches[state] if state < len(matches) else None

    _prev_completer = readline.get_completer()
    _prev_delims = readline.get_completer_delims()
    readline.set_completer(_path_completer)
    readline.set_completer_delims(" \t\n")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")

    try:
        while True:
            # Scan Downloads on every iteration (catches newly downloaded files)
            _detected = _scan_downloads()
            if _detected:
                _selected = _offer_detected(_detected)
                if _selected:
                    response = _selected
                else:
                    # User declined detected files — fall through to manual input
                    try:
                        response = input("Path to client_secret.json (or paste Client Id): ")
                    except EOFError:
                        response = ""
            else:
                try:
                    response = input("Path or Client ID (Enter to re-scan ~/Downloads): ")
                except EOFError:
                    response = ""

            if not response:
                # Empty input = re-scan Downloads on next iteration
                click.secho("  Scanning ~/Downloads...", dim=True)
                continue

            response = response.strip()
            json_path = Path(response).expanduser()

            if json_path.suffix == ".json" or json_path.exists():
                # JSON file path
                if not json_path.exists():
                    click.echo(f"File not found: {json_path}")
                    continue
                try:
                    raw_text = json_path.read_text()
                    data = json_mod.loads(raw_text)
                    inner = data.get("installed") or data.get("web") or data
                    client_id = inner["client_id"].replace("\n", "").replace("\r", "")
                    client_secret = inner["client_secret"].replace("\n", "").replace("\r", "")
                except (json_mod.JSONDecodeError, KeyError, TypeError, OSError) as e:
                    click.echo(f"Invalid client_secret.json: {e}")
                    continue
                # Copy JSON to credentials dir for workspace-mcp (0o600 from creation)
                dest = get_google_credentials_dir() / "client_secret.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(raw_text)
                click.secho(f"  ✓ Copied {json_path.name} to {dest}", fg="green")
            else:
                # User pasted a Client ID directly — strip newlines to prevent
                # format injection into client_env file (matches config_set pattern)
                client_id = response.replace("\n", "").replace("\r", "")
                client_secret = (
                    click.prompt("Google OAuth Client Secret", default="", show_default=False)
                    .replace("\n", "")
                    .replace("\r", "")
                )
                if not client_secret:
                    click.echo("Client Secret is required.")
                    continue

            break
    finally:
        # Restore readline state even on KeyboardInterrupt
        readline.set_completer(_prev_completer)
        readline.set_completer_delims(_prev_delims)

    # Save credentials (atomic write with 0o600 from creation — no world-readable window)
    creds_dir = get_google_credentials_dir()
    creds_dir.mkdir(parents=True, exist_ok=True)
    secrets_file = creds_dir / "client_env"
    fd = os.open(secrets_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"GOOGLE_OAUTH_CLIENT_ID={client_id}\nGOOGLE_OAUTH_CLIENT_SECRET={client_secret}\n")
    completed[4] = "saved"

    # Final success screen
    _setup_header(4, completed, skip_clear=False)
    click.secho("  ✓ Credentials saved.", fg="green")
    click.secho(f"    {secrets_file}", dim=True)
    click.echo()
    click.echo("Run `summon auth google login` to authenticate.")


# Read-only by default.  Append `:rw` to a service name to opt into write
# scopes (e.g. "calendar:rw").  This keeps the consent screen minimal while
# still being compatible with workspace-mcp's has_required_scopes() hierarchy.
_GOOGLE_SCOPE_PREFIX = "https://www.googleapis.com/auth/"
_GOOGLE_SERVICE_SCOPES: dict[str, dict[str, list[str]]] = {
    "gmail": {
        "ro": ["gmail.readonly"],
        "rw": ["gmail.modify", "gmail.settings.basic"],
    },
    "drive": {
        "ro": ["drive.readonly"],
        "rw": ["drive"],
    },
    "calendar": {
        "ro": ["calendar.readonly"],
        "rw": ["calendar"],
    },
}
_GOOGLE_BASE_SCOPES = [
    "openid",
    f"{_GOOGLE_SCOPE_PREFIX}userinfo.email",
    f"{_GOOGLE_SCOPE_PREFIX}userinfo.profile",
]


def _google_scopes_for_services(services: list[str]) -> list[str]:
    """Build a minimal OAuth scope list from service specs.

    Each entry is ``"service"`` (read-only) or ``"service:rw"`` (read-write).
    Unknown services are silently skipped so the caller can validate separately.
    """
    scopes: list[str] = list(_GOOGLE_BASE_SCOPES)
    for spec in services:
        name, _, mode = spec.partition(":")
        tier = "rw" if mode == "rw" else "ro"
        entry = _GOOGLE_SERVICE_SCOPES.get(name)
        if entry:
            for s in entry[tier]:
                full = s if s.startswith("https://") else f"{_GOOGLE_SCOPE_PREFIX}{s}"
                if full not in scopes:
                    scopes.append(full)
    return scopes


def _describe_granted_scopes(granted: set[str]) -> str:
    """Return a short human summary of granted Google scopes."""
    parts: list[str] = []
    for svc, tiers in _GOOGLE_SERVICE_SCOPES.items():
        rw_scopes = {
            s if s.startswith("https://") else f"{_GOOGLE_SCOPE_PREFIX}{s}" for s in tiers["rw"]
        }
        ro_scopes = {
            s if s.startswith("https://") else f"{_GOOGLE_SCOPE_PREFIX}{s}" for s in tiers["ro"]
        }
        if rw_scopes & granted:
            parts.append(f"{svc} (read-write)")
        elif ro_scopes & granted:
            parts.append(f"{svc} (read-only)")
    return ", ".join(parts)


_GOOGLE_WRITE_PROMPTS: dict[str, str] = {
    "gmail": "Send and compose emails via Gmail",
    "calendar": "Create and edit Google Calendar events",
    "drive": "Create, edit, and delete Google Drive files",
}


def _load_google_client_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) or sys.exit."""
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

    if not (client_id and client_secret):
        secrets_file = get_google_credentials_dir() / "client_env"
        if secrets_file.exists():
            for line in secrets_file.read_text().splitlines():
                if line.startswith("GOOGLE_OAUTH_CLIENT_ID="):
                    client_id = line.split("=", 1)[1].strip()
                elif line.startswith("GOOGLE_OAUTH_CLIENT_SECRET="):
                    client_secret = line.split("=", 1)[1].strip()

    if not (client_id and client_secret):
        click.echo("No Google OAuth credentials configured.", err=True)
        click.echo("Run `summon auth google setup` to create and configure credentials.", err=True)
        sys.exit(1)

    return client_id, client_secret


def _secure_credential_files(creds_dir: Path) -> None:
    """Ensure all credential JSON files are owner-readable only (0600)."""
    for p in creds_dir.glob("*.json"):
        p.chmod(0o600)


def _run_google_oauth(
    client_id: str,
    client_secret: str,
    scopes: list[str],
) -> Any:
    """Open a browser, complete the OAuth flow, and return credentials."""
    from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    import warnings  # noqa: PLC0415

    click.echo("Opening browser for Google authorization...\n")
    try:
        flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="datetime.datetime.utcfromtimestamp",
                category=DeprecationWarning,
            )
            return flow.run_local_server(
                port=0,
                open_browser=True,
                prompt="consent",
                access_type="offline",
            )
    except Exception as e:
        click.echo(f"Google auth flow failed: {e}", err=True)
        sys.exit(1)


def _resolve_google_email(cred: Any) -> str:
    """Discover the authenticated user's email, falling back to 'default'."""
    try:
        from googleapiclient.discovery import build  # noqa: PLC0415

        svc = build("oauth2", "v2", credentials=cred)
        return svc.userinfo().get().execute().get("email", "default")
    except Exception:
        return "default"


def google_auth() -> None:
    """Interactive Google Workspace authentication."""
    try:
        from auth.credential_store import LocalDirectoryCredentialStore  # noqa: PLC0415
        from auth.google_auth import has_required_scopes  # noqa: PLC0415
    except ImportError:
        click.echo(
            "Google Workspace support requires the 'google' extra: "
            "uv pip install summon-claude[google]",
            err=True,
        )
        sys.exit(1)

    client_id, client_secret = _load_google_client_credentials()
    creds_dir = get_google_credentials_dir()
    store = LocalDirectoryCredentialStore(str(creds_dir))

    # Load existing credential to derive prompt defaults.
    existing_cred = None
    users = store.list_users()
    if users:
        existing_cred = store.get_credential(users[0])

    # Detect which services already have write access.
    granted = set(existing_cred.scopes or []) if existing_cred else set()
    existing_rw: set[str] = set()
    for svc, tiers in _GOOGLE_SERVICE_SCOPES.items():
        rw_scopes = {
            s if s.startswith("https://") else f"{_GOOGLE_SCOPE_PREFIX}{s}" for s in tiers["rw"]
        }
        if rw_scopes & granted:
            existing_rw.add(svc)

    # Ask which services need write access.
    click.echo("All services get read-only access by default.")
    click.echo("Grant write access to any of these?\n")
    services: list[str] = []
    for svc, desc in _GOOGLE_WRITE_PROMPTS.items():
        default = svc in existing_rw
        if click.confirm(f"  {desc}", default=default):
            services.append(f"{svc}:rw")
        else:
            services.append(svc)
    click.echo()

    scopes = _google_scopes_for_services(services)

    # If valid credentials exist, check whether they exactly match the
    # requested scopes.  Re-auth if scopes were added OR removed.
    need_reauth = True
    if existing_cred:
        from google.auth.transport.requests import Request  # noqa: PLC0415

        # Refresh if expired.
        if existing_cred.expired and existing_cred.refresh_token:
            try:
                existing_cred.refresh(Request())
                store.store_credential(users[0], existing_cred)
                _secure_credential_files(creds_dir)
            except Exception:
                existing_cred = None  # force re-auth below

        if existing_cred and existing_cred.valid:
            requested_set = set(scopes)
            if has_required_scopes(granted, scopes) and requested_set == (granted & requested_set):
                # Granted scopes cover everything requested AND the user
                # didn't narrow any service (e.g. drop calendar write).
                need_reauth = False
            elif has_required_scopes(granted, scopes):
                # Granted scopes are broader than requested — user is
                # dropping write access.  Re-auth with the smaller set.
                click.echo("Re-authenticating to narrow scope access.\n")
            else:
                click.echo("Current credentials are missing some requested scopes.\n")

    if not need_reauth:
        click.echo(f"Google credentials for {users[0]} already cover the requested scopes.")
    else:
        cred = _run_google_oauth(client_id, client_secret, scopes)
        user_email = _resolve_google_email(cred)
        store.store_credential(user_email, cred)
        _secure_credential_files(creds_dir)
        click.echo()
        click.echo(f"Google Workspace authenticated as {user_email}.")
        click.echo(f"Credentials stored in {creds_dir}")

    # Context-aware next-step guidance.
    from summon_claude.cli.config import parse_env_file  # noqa: PLC0415
    from summon_claude.daemon import is_daemon_running  # noqa: PLC0415

    click.echo()
    values = parse_env_file(get_config_file())
    scribe_on = values.get("SUMMON_SCRIBE_ENABLED", "").lower() in ("true", "1", "yes")
    daemon_up = is_daemon_running()
    if scribe_on and daemon_up:
        click.echo("Scribe will pick up Google tools on next project restart:")
        click.echo("  summon project down && summon project up")
    elif scribe_on:
        click.echo("Scribe will use Google tools on next start:")
        click.echo("  summon project up")
    else:
        click.echo("To use Google tools, enable the scribe agent:")
        click.echo("  summon config set SUMMON_SCRIBE_ENABLED true")
        click.echo("  summon project up")


def _check_google_status(
    *,
    prefix: str = "",
    quiet: bool = False,
) -> bool | None:
    """Check Google Workspace authentication status.

    Returns True if valid, False if credentials exist but are broken,
    or None if Google isn't configured (not an error, just absent).
    """
    try:
        from auth.credential_store import LocalDirectoryCredentialStore  # noqa: PLC0415
    except ImportError:
        if not quiet:
            click.echo(f"{prefix}[INFO] Google: not installed (install summon-claude[google])")
        return None

    creds_dir = get_google_credentials_dir()
    if not creds_dir.exists():
        if not quiet:
            click.echo(f"{prefix}[INFO] Google: not configured (run `summon auth google setup`)")
        return None

    store = LocalDirectoryCredentialStore(str(creds_dir))
    users = store.list_users()
    if not users:
        if not quiet:
            click.echo(f"{prefix}[INFO] Google: no credentials found")
        return None

    all_ok = True
    for user in users:
        cred = store.get_credential(user)
        if not cred:
            if not quiet:
                click.echo(f"{prefix}[FAIL] Google: invalid credential file ({user})")
            all_ok = False
            continue

        if cred.valid:
            status = "valid"
        elif cred.expired and cred.refresh_token:
            status = "expired (will refresh on next use)"
        else:
            if not quiet:
                click.echo(
                    f"{prefix}[FAIL] Google: invalid — re-run `summon auth google login` ({user})"
                )
            all_ok = False
            continue

        if not quiet:
            # Summarise granted access level per service.
            granted = set(cred.scopes or [])
            access = _describe_granted_scopes(granted)
            click.echo(f"{prefix}[PASS] Google: {status} ({user})")
            if access:
                click.echo(f"{prefix}  Access: {access}")

    return all_ok


def google_status() -> None:
    """Check Google Workspace authentication status (CLI entry point)."""
    _check_google_status()
