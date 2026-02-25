"""Command dispatch — intercept !-prefixed Slack messages for local handling or pass-through."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from summon_claude.providers.base import ChatProvider

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

    channel_id: str
    thread_ts: str | None
    user_id: str
    provider: ChatProvider
    turns: int = 0
    cost_usd: float = 0.0
    start_time: datetime | None = None
    model: str | None = None
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


HandlerFn = Callable[[list[str], CommandContext], Awaitable[CommandResult]]


class CommandRegistry:
    """Registry for !-prefixed command dispatch."""

    _REMAP_TO_END: frozenset[str] = frozenset({"quit", "exit", "logout"})
    _BLOCKED_COMMANDS: dict[str, str] = {
        "login": "Not available in Slack sessions.",
        # CLI-only system commands — output goes to terminal, not SDK stream
        "compact": "CLI-only (output not available via SDK). Context is managed automatically.",
        "context": "CLI-only (output not available via SDK). Use `!status` for session info.",
        "cost": "CLI-only (output not available via SDK). Use `!status` for cost info.",
        "release-notes": "CLI-only (output not available via SDK).",
    }

    def __init__(self) -> None:
        self._local: dict[str, tuple[HandlerFn, str]] = {}
        self._passthrough: dict[str, str] = {}

    def register(self, name: str, handler: HandlerFn, description: str) -> None:
        """Register a local command handler."""
        self._local[name] = (handler, description)

    def set_passthrough_commands(self, commands: list[Any]) -> None:
        """Populate passthrough commands from SDK init response."""
        self._passthrough = {}
        for item in commands:
            if isinstance(item, str):
                name = item.lstrip("/")
                if name and name not in self._BLOCKED_COMMANDS and name not in self._REMAP_TO_END:
                    self._passthrough[name] = f"/{name}"
            elif isinstance(item, dict):
                name = item.get("name", "").lstrip("/")
                description = item.get("description", f"/{name}")
                if name and name not in self._BLOCKED_COMMANDS and name not in self._REMAP_TO_END:
                    self._passthrough[name] = description

    def parse(self, text: str) -> tuple[str, list[str]] | None:
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

    async def dispatch(self, name: str, args: list[str], context: CommandContext) -> CommandResult:
        """Dispatch a command: remap -> blocked -> local -> passthrough -> unknown."""
        # Remap aliases to !end
        if name in self._REMAP_TO_END:
            name = "end"

        # Blocked commands
        if name in self._BLOCKED_COMMANDS:
            reason = self._BLOCKED_COMMANDS[name]
            return CommandResult(text=f":no_entry: `!{name}` is not available: {reason}")

        # Local commands
        if name in self._local:
            handler, _ = self._local[name]
            return await handler(args, context)

        # Passthrough to Claude
        if name in self._passthrough:
            return CommandResult(text=None, suppress_queue=False)

        # Unknown
        return CommandResult(
            text=f":question: Unknown command `!{name}`. Use `!help` to see available commands."
        )

    def all_commands(self) -> dict[str, str]:
        """Return combined dict of all commands (local + passthrough + remap aliases)."""
        result: dict[str, str] = {}
        for name, (_, description) in self._local.items():
            result[name] = description
        for name, description in self._passthrough.items():
            result[name] = description
        for alias in self._REMAP_TO_END:
            result[alias] = "Alias for !end"
        return result

    def register_passthrough(self, name: str, description: str) -> None:
        """Register a single passthrough command (for pre-SDK-init fallbacks)."""
        if name not in self._BLOCKED_COMMANDS and name not in self._REMAP_TO_END:
            self._passthrough[name] = description

    def local_commands(self) -> list[str]:
        """Return sorted list of local command names."""
        return sorted(self._local)

    def passthrough_commands(self) -> list[str]:
        """Return sorted list of passthrough command names."""
        return sorted(self._passthrough)


# ------------------------------------------------------------------
# Built-in handlers (module-level async functions)
# ------------------------------------------------------------------


_CORE_CLI_COMMANDS: frozenset[str] = frozenset(
    {
        "compact",
        "context",
        "cost",
        "config",
        "doctor",
        "model",
        "review",
        "bug",
        "init",
        "upgrade",
    }
)


async def _handle_help(args: list[str], ctx: CommandContext) -> CommandResult:
    registry: CommandRegistry = ctx.metadata["registry"]

    # Detail view: passthrough to CLI's /help for rich output
    if args:
        return CommandResult(text=None, suppress_queue=False)

    # Overview: grouped local summary
    local_names = sorted(registry.local_commands())
    passthrough_names = sorted(registry.passthrough_commands())

    core_names = sorted(n for n in passthrough_names if n in _CORE_CLI_COMMANDS)
    plugin_names = sorted(n for n in passthrough_names if n not in _CORE_CLI_COMMANDS)

    lines: list[str] = ["*Session* (local): " + ", ".join(f"`!{n}`" for n in local_names)]

    if core_names:
        lines.append("*Core CLI*: " + ", ".join(f"`!{n}`" for n in core_names))

    if plugin_names:
        lines.append("*Plugin*: " + ", ".join(f"`!{n}`" for n in plugin_names))

    lines.append("_Use `!command` to run. Passthrough commands use Claude's native handling._")

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
    return CommandResult(text=None, suppress_queue=False, metadata={"clear": True})


def build_registry() -> CommandRegistry:
    """Factory that creates a CommandRegistry with all built-in handlers registered."""
    registry = CommandRegistry()
    registry.register("help", _handle_help, "Show available commands")
    registry.register("status", _handle_status, "Show session status")
    registry.register("end", _handle_end, "End this session")
    registry.register("clear", _handle_clear, "Clear conversation history")
    # Ensure model is available as passthrough even before SDK init populates the list
    registry.register_passthrough("model", "Switch or display the active model")
    return registry
