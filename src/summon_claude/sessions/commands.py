"""Command dispatch — intercept !-prefixed Slack messages for local handling or pass-through."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from summon_claude.config import PluginSkill

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result from dispatching a command."""

    text: str | None
    suppress_queue: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandContext:
    """Context passed to command handlers."""

    turns: int = 0
    cost_usd: float = 0.0
    start_time: datetime | None = None
    model: str | None = None
    effort: str = "high"
    session_id: str = ""
    auto_enabled: bool = False
    in_worktree: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


HandlerFn = Callable[[list[str], CommandContext], Awaitable[CommandResult]]


@dataclass
class CommandDef:
    """Declarative definition for a single command."""

    description: str
    handler: HandlerFn | None = None
    block_reason: str | None = None
    aliases: list[str] = field(default_factory=list)
    max_args: int | None = None
    argument_hint: str = ""


@dataclass
class CommandMatch:
    """A command found in message text by ``find_commands``."""

    prefix: str  # always "!"
    name: str  # canonical (after alias resolution)
    raw_name: str  # as typed
    args: list[str]  # consumed args (LOCAL with max_args)
    start: int  # position in text
    end: int  # end position (including consumed args)


# ------------------------------------------------------------------
# Built-in handlers (module-level async functions)
# ------------------------------------------------------------------


async def _handle_help(args: list[str], _ctx: CommandContext) -> CommandResult:  # noqa: PLR0912
    # Build skill grouping (used by both detail and listing paths)
    skill_grouped: dict[str, list[str]] = {}
    for n, d in COMMAND_ACTIONS.items():
        if not d.handler and not d.block_reason and ":" in n:
            plugin, _, skill = n.partition(":")
            skill_grouped.setdefault(plugin, []).append(skill)

    if args:
        cmd_name = args[0].lower().lstrip("!")
        canonical = _ALIAS_LOOKUP.get(cmd_name, cmd_name)

        # Check if arg is a plugin name — list its skills
        if cmd_name in skill_grouped:
            skills = sorted(skill_grouped[cmd_name])
            lines = [f"*{cmd_name}* plugin skills:"]
            lines.extend(f"  `!{cmd_name}:{s}`" for s in skills)
            return CommandResult(text="\n".join(lines))

        defn = COMMAND_ACTIONS.get(canonical)
        if defn is None:
            return CommandResult(text=f":question: Unknown command `!{cmd_name}`.")

        if defn.handler:
            action = "local"
        elif defn.block_reason:
            action = "blocked"
        elif ":" in canonical:
            action = "skill"
        else:
            action = "passthrough"
        safe_hint = defn.argument_hint.replace("`", "'")
        usage = f"`!{canonical} {safe_hint}`" if safe_hint.strip() else f"`!{canonical}`"
        lines = [f"*{usage}* — {defn.description}", f"_Type: {action}_"]
        if defn.aliases:
            lines.append(f"_Aliases: {', '.join(f'`!{a}`' for a in defn.aliases)}_")
        # Show short aliases from _ALIAS_LOOKUP (e.g. plugin skills)
        short_aliases = [a for a, target in _ALIAS_LOOKUP.items() if target == canonical]
        if short_aliases:
            lines.append(f"_Short: {', '.join(f'`!{a}`' for a in sorted(short_aliases))}_")
        return CommandResult(text="\n".join(lines))

    local_names = sorted(n for n, d in COMMAND_ACTIONS.items() if d.handler)

    # Filter plugin skills out of passthrough list — they belong under their plugin
    plugin_skill_short_names = set()
    for alias, target in _ALIAS_LOOKUP.items():
        if ":" in target:
            plugin_skill_short_names.add(alias)
    passthrough_names = sorted(
        n
        for n, d in COMMAND_ACTIONS.items()
        if not d.handler
        and not d.block_reason
        and ":" not in n
        and n not in plugin_skill_short_names
    )

    lines = ["*Session (local):* " + ", ".join(f"`!{n}`" for n in local_names)]
    if passthrough_names:
        lines.append("")
        lines.append("*Claude CLI:* " + ", ".join(f"`!{n}`" for n in passthrough_names))
    if skill_grouped:
        lines.append("")
        parts = [f"`{p}` ({len(ss)})" for p, ss in sorted(skill_grouped.items())]
        lines.append("*Installed Plugins:* " + ", ".join(parts))
        lines.append("_Use `!help PLUGIN` to list a plugin's skills._")
    lines.append("")
    lines.append("_Use `!command` to run. `!help COMMAND` for details._")

    return CommandResult(text="\n".join(lines))


async def _handle_status(_args: list[str], ctx: CommandContext) -> CommandResult:
    if ctx.start_time is not None:
        elapsed = datetime.now(UTC) - ctx.start_time
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"
    else:
        uptime = "unknown"

    model_display = ctx.model or "unknown"
    lines = [
        "*Session Status*",
        f"  Model: `{model_display}`",
        f"  Effort: `{ctx.effort}`",
        f"  Session ID: `{ctx.session_id}`",
        f"  Turns: {ctx.turns}",
        f"  Cost: ${ctx.cost_usd:.4f}",
        f"  Uptime: {uptime}",
    ]
    return CommandResult(text="\n".join(lines))


async def _handle_end(_args: list[str], _ctx: CommandContext) -> CommandResult:
    return CommandResult(
        text=":wave: Ending session...",
        metadata={"shutdown": True},
    )


async def _handle_clear(_args: list[str], _ctx: CommandContext) -> CommandResult:
    return CommandResult(text=":broom: Conversation cleared.", metadata={"clear": True})


async def _handle_stop(_args: list[str], _ctx: CommandContext) -> CommandResult:
    return CommandResult(
        text=":octagonal_sign: Cancelling current turn...",
        metadata={"stop": True},
    )


async def _handle_model(args: list[str], ctx: CommandContext) -> CommandResult:
    models = ctx.metadata.get("models", [])
    valid_names = [m.get("value", m) if isinstance(m, dict) else str(m) for m in models]

    if not args:
        current = ctx.model or "unknown"
        lines = [f"*Current model:* `{current}`"]
        if valid_names:
            lines.append("*Available:* " + ", ".join(f"`{n}`" for n in valid_names))
        return CommandResult(text="\n".join(lines))

    requested = args[0]
    if valid_names and requested not in valid_names:
        return CommandResult(
            text=f":warning: Unknown model `{requested}`. "
            f"Available: {', '.join(f'`{n}`' for n in valid_names)}"
        )

    return CommandResult(
        text=f":gear: Switching to `{requested}`...",
        metadata={"set_model": requested},
    )


async def _handle_effort(args: list[str], ctx: CommandContext) -> CommandResult:
    valid = ("low", "medium", "high", "max")
    if not args:
        return CommandResult(
            text=f"*Current effort:* `{ctx.effort}`\n"
            f"*Available:* {', '.join(f'`{v}`' for v in valid)}"
        )
    requested = args[0].lower()
    if requested not in valid:
        return CommandResult(
            text=f":warning: Unknown effort `{requested}`. "
            f"Available: {', '.join(f'`{v}`' for v in valid)}"
        )
    return CommandResult(
        text=f":gear: Switching effort to `{requested}`...",
        metadata={"set_effort": requested},
    )


async def _handle_compact(args: list[str], _ctx: CommandContext) -> CommandResult:
    return CommandResult(
        text=None,
        metadata={"compact": True, "instructions": " ".join(args) if args else None},
    )


async def _handle_summon(args: list[str], _ctx: CommandContext) -> CommandResult:
    if not args:
        return CommandResult(text="Usage: `!summon start` | `!summon resume [session-id]`")
    match args[0].lower():
        case "start":
            return CommandResult(text=None, metadata={"spawn": True})
        case "resume":
            target = args[1] if len(args) > 1 else None
            return CommandResult(
                text=None,
                metadata={"resume": True, "resume_target": target},
            )
        case _:
            return CommandResult(
                text=(
                    f":question: Unknown subcommand `{args[0]}`. "
                    "Usage: `!summon start` | `!summon resume [session-id]`"
                )
            )


async def _handle_diff(args: list[str], _ctx: CommandContext) -> CommandResult:
    if not args:
        return CommandResult(text=None, metadata={"diff_all": True})
    return CommandResult(text=None, metadata={"diff_file": args[0]})


async def _handle_show(args: list[str], _ctx: CommandContext) -> CommandResult:
    if not args:
        return CommandResult(text="Usage: `!show <file>`")
    return CommandResult(text=None, metadata={"show_file": args[0]})


async def _handle_changes(_args: list[str], _ctx: CommandContext) -> CommandResult:
    return CommandResult(text=None, metadata={"show_changes": True})


async def _handle_auto(args: list[str], ctx: CommandContext) -> CommandResult:
    if not args:
        status = "enabled" if ctx.auto_enabled else "disabled"
        return CommandResult(
            text=f"*Auto-mode classifier:* `{status}`\n"
            "Use `!auto on` or `!auto off` to toggle.\n"
            "Use `!auto rules` to see effective rules.",
            metadata={"standalone": True},
        )
    action = args[0].lower()
    if action == "on":
        if not ctx.in_worktree:
            return CommandResult(
                text=":warning: Auto-mode requires write access. "
                "Enter a worktree first (`EnterWorktree`).",
                metadata={"standalone": True},
            )
        return CommandResult(
            text=":gear: Enabling auto-mode classifier...",
            metadata={"set_auto": True, "standalone": True},
        )
    if action == "off":
        return CommandResult(
            text=":gear: Disabling auto-mode classifier. Tool calls will require Slack approval.",
            metadata={"set_auto": False, "standalone": True},
        )
    if action == "rules":
        from summon_claude.sessions.classifier import (  # noqa: PLC0415
            get_effective_allow_rules,
            get_effective_deny_rules,
        )

        deny = get_effective_deny_rules(ctx.metadata.get("auto_mode_deny", ""))
        allow = get_effective_allow_rules(ctx.metadata.get("auto_mode_allow", ""))
        return CommandResult(
            text=f"*Block rules:*\n```\n{deny}\n```\n\n*Allow rules:*\n```\n{allow}\n```",
            metadata={"standalone": True},
        )
    return CommandResult(
        text=f":warning: Unknown auto-mode action `{action}`. Use `on`, `off`, or `rules`.",
        metadata={"standalone": True},
    )


# ------------------------------------------------------------------
# Shared block-reason constant
# ------------------------------------------------------------------

_CLI_ONLY = "Only available in the interactive CLI"

# ------------------------------------------------------------------
# Declarative command inventory
# ------------------------------------------------------------------

COMMAND_ACTIONS: dict[str, CommandDef] = {
    # --- Local handlers ---
    "help": CommandDef(
        description="Show available commands",
        handler=_handle_help,
        max_args=1,
    ),
    "status": CommandDef(
        description="Show session status",
        handler=_handle_status,
        max_args=0,
    ),
    "end": CommandDef(
        description="End this session",
        handler=_handle_end,
        max_args=0,
        aliases=["quit", "exit", "logout"],
    ),
    "clear": CommandDef(
        description="Clear conversation history",
        handler=_handle_clear,
        max_args=0,
        aliases=["new", "reset"],
    ),
    "stop": CommandDef(
        description="Cancel the current Claude turn",
        handler=_handle_stop,
        max_args=0,
    ),
    "model": CommandDef(
        description="Switch or display the active model",
        handler=_handle_model,
        max_args=1,
    ),
    "effort": CommandDef(
        description="Switch or display the effort level",
        handler=_handle_effort,
        max_args=1,
    ),
    "auto": CommandDef(
        description="Toggle or inspect auto-mode classifier",
        handler=_handle_auto,
        max_args=1,
        aliases=["automode"],
        argument_hint="[on|off|rules]",
    ),
    "compact": CommandDef(
        description="Compact conversation context",
        handler=_handle_compact,
        max_args=None,
    ),
    "summon": CommandDef(
        description="Spawn or resume a session",
        handler=_handle_summon,
        max_args=2,
    ),
    # --- Passthrough (forwarded to Claude CLI) ---
    "review": CommandDef(description="Review code changes"),
    "init": CommandDef(description="Initialize project configuration"),
    "pr-comments": CommandDef(description="Review PR comments"),
    "security-review": CommandDef(description="Run security review"),
    "debug": CommandDef(description="Debug session issues"),
    "claude-developer-platform": CommandDef(description="Claude developer platform info"),
    "simplify": CommandDef(description="Simplify and refine code"),
    # --- Blocked with specific reasons ---
    "insights": CommandDef(
        description="Generates a local HTML report",
        block_reason="Generates a local HTML report — not viewable in Slack",
    ),
    "context": CommandDef(
        description="Show context info",
        block_reason="Use `!status` for context info",
    ),
    "cost": CommandDef(
        description="Show cost info",
        block_reason="Use `!status` for cost info",
    ),
    "release-notes": CommandDef(
        description="Show release notes",
        block_reason="Not available in Slack sessions",
    ),
    "login": CommandDef(
        description="Log in to Claude",
        block_reason="Not available in Slack sessions",
    ),
    # --- Blocked CLI-only ---
    "config": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY, aliases=["settings"]),
    "doctor": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "desktop": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY, aliases=["app"]),
    "feedback": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY, aliases=["bug"]),
    "permissions": CommandDef(
        description=_CLI_ONLY, block_reason=_CLI_ONLY, aliases=["allowed-tools"]
    ),
    "mobile": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY, aliases=["ios", "android"]),
    "resume": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY, aliases=["continue"]),
    "rewind": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY, aliases=["checkpoint"]),
    "remote-control": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY, aliases=["rc"]),
    "add-dir": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "agents": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "batch": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "claude-api": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "chrome": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "copy": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "diff": CommandDef(
        description="Show git diff (all changes, or specify a file)",
        handler=_handle_diff,
        max_args=1,
        argument_hint="[file_path]",
    ),
    "show": CommandDef(
        description="Show current file contents",
        handler=_handle_show,
        max_args=1,
        argument_hint="<file>",
    ),
    "changes": CommandDef(
        description="Show all files changed in this session",
        handler=_handle_changes,
        max_args=0,
    ),
    "export": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "extra-usage": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "fast": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "fork": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "heapdump": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "hooks": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "ide": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "install-github-app": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "install-slack-app": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "keybindings": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "mcp": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "memory": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "output-style": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "passes": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "plan": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "plugin": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "privacy-settings": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "reload-plugins": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "remote-env": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "rename": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "sandbox": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "skills": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "stats": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "statusline": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "stickers": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "tasks": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "terminal-setup": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "theme": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "upgrade": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "usage": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
    "vim": CommandDef(description=_CLI_ONLY, block_reason=_CLI_ONLY),
}

# ------------------------------------------------------------------
# Alias lookup — derived at import time
# ------------------------------------------------------------------

_ALIAS_LOOKUP: dict[str, str] = {
    alias: name for name, defn in COMMAND_ACTIONS.items() for alias in defn.aliases
}


# ------------------------------------------------------------------
# SDK command validation
# ------------------------------------------------------------------


def validate_sdk_commands(sdk_commands: Sequence[dict[str, Any] | str]) -> list[str]:
    """Check SDK commands against COMMAND_ACTIONS; log warnings for unknowns.

    Stores ``argumentHint`` from SDK response into matching CommandDef entries.
    Unknown SDK commands are added as passthrough so they don't break dispatch.

    Returns the list of unknown command names.
    """
    unknown: list[str] = []

    for item in sdk_commands:
        if isinstance(item, str):
            name = item.lstrip("/")
            hint = ""
        elif isinstance(item, dict):
            name = item.get("name", "").lstrip("/")
            hint = item.get("argumentHint", "")
        else:
            continue

        if not name:
            continue

        # Validate command name format (alphanumeric, hyphens, underscores, colons)
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_:-]{0,63}$", name):
            logger.warning("SDK command '%s' has invalid name format — skipping", name)
            continue

        defn = COMMAND_ACTIONS.get(name)
        if defn is not None:
            if hint and not defn.argument_hint:
                defn.argument_hint = hint
        else:
            logger.warning("SDK command '%s' not in COMMAND_ACTIONS — add it", name)
            unknown.append(name)
            # Register as passthrough so it still works
            COMMAND_ACTIONS[name] = CommandDef(
                description=hint or f"/{name}",
                argument_hint=hint,
            )

    return unknown


def register_plugin_skills(skills: Sequence[PluginSkill]) -> int:
    """Register discovered plugin skills/commands as passthrough entries.

    Adds both the fully-qualified name (``plugin:skill``) and, if unambiguous,
    a short alias (``skill``) so users can reference either form.

    Returns the number of newly registered entries.
    """
    registered = 0
    short_names: dict[str, str] = {}  # short_name → plugin_name (for collision detection)

    for skill in skills:
        fq_name = f"{skill.plugin_name}:{skill.name}"
        if fq_name not in COMMAND_ACTIONS:
            COMMAND_ACTIONS[fq_name] = CommandDef(description=skill.description or fq_name)
            registered += 1

        # Track short-name ownership for alias registration
        if skill.name in short_names:
            short_names[skill.name] = ""  # collision — mark ambiguous
        else:
            short_names[skill.name] = skill.plugin_name

    # Register unambiguous short-name aliases
    for short_name, plugin in short_names.items():
        if not plugin:
            continue  # ambiguous — skip
        if short_name in COMMAND_ACTIONS or short_name in _ALIAS_LOOKUP:
            continue  # already a built-in, SDK command, or registered alias
        fq_name = f"{plugin}:{short_name}"
        _ALIAS_LOOKUP[short_name] = fq_name
        registered += 1

    logger.info("Registered %d plugin skill entries", registered)
    return registered


# ------------------------------------------------------------------
# Module-level dispatch and parse functions
# ------------------------------------------------------------------


async def dispatch(name: str, args: list[str], context: CommandContext) -> CommandResult:
    """Dispatch a command: alias -> blocked -> local -> passthrough -> unknown."""
    canonical = _ALIAS_LOOKUP.get(name, name)

    defn = COMMAND_ACTIONS.get(canonical)
    if defn is None:
        return CommandResult(
            text=f":question: Unknown command `!{name}`. Use `!help` to see available commands."
        )

    if defn.block_reason:
        return CommandResult(text=f":no_entry: `!{name}` is not available: {defn.block_reason}")

    if defn.handler:
        return await defn.handler(args, context)

    # Passthrough
    return CommandResult(text=None, suppress_queue=False)


def parse(text: str) -> tuple[str, list[str]] | None:
    """Detect ! prefix and split into (command, args_list). Returns None if not a command."""
    if not text.startswith("!"):
        return None
    rest = text[1:]
    if not rest or not rest[0].isalpha():
        return None
    parts = rest.split()
    command = parts[0].lower()
    if len(command) > 64:
        return None
    args = parts[1:]
    return command, args


# ------------------------------------------------------------------
# Mid-message command detection
# ------------------------------------------------------------------

# Match !cmd after whitespace or start-of-string.
# Negative lookbehind (?<![:/]) prevents matching inside URLs (https://...)
# or path-like tokens (repo/review).  Slash (/) is NOT a valid prefix.
# Colons allowed for plugin:skill-name syntax (e.g. !dev-essentials:session-start).
_COMMAND_RE = re.compile(r"(?:^|(?<=\s))(?<![:/])(!)([a-zA-Z][a-zA-Z0-9_:-]{0,63})")


def find_commands(text: str) -> list[CommandMatch]:
    """Find all !cmd tokens in text, with alias resolution and arg consumption."""
    matches: list[CommandMatch] = []
    consumed_ranges: list[tuple[int, int]] = []

    for m in _COMMAND_RE.finditer(text):
        # Skip if this match falls within a previously consumed arg range
        if any(start <= m.start() < end for start, end in consumed_ranges):
            continue

        prefix = m.group(1)
        raw_name = m.group(2).lower()
        canonical = _ALIAS_LOOKUP.get(raw_name, raw_name)
        defn = COMMAND_ACTIONS.get(canonical)

        cmd_end = m.end()
        consumed_args: list[str] = []

        # Consume args for LOCAL commands with max_args > 0
        if defn and defn.handler and defn.max_args is not None and defn.max_args > 0:
            remaining = text[cmd_end:]
            for arg_m in re.finditer(r"\S+", remaining):
                if len(consumed_args) >= defn.max_args:
                    break
                word = arg_m.group()
                # Stop at next command prefix
                if len(word) > 1 and word[0] in "!/" and word[1].isalpha():
                    break
                consumed_args.append(word)
                cmd_end = m.end() + arg_m.end()
            if consumed_args:
                consumed_ranges.append((m.end(), cmd_end))

        matches.append(
            CommandMatch(
                prefix=prefix,
                name=canonical,
                raw_name=raw_name,
                args=consumed_args,
                start=m.start(),
                end=cmd_end,
            )
        )

    return matches
