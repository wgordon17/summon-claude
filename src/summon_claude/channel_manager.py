"""Slack channel lifecycle management — create, configure, and archive session channels."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from summon_claude.context import ContextUsage
from summon_claude.providers.base import ChatProvider

logger = logging.getLogger(__name__)

_MAX_CHANNEL_NAME_LEN = 80


class ChannelManager:
    """Manages Slack channel creation and lifecycle for summon sessions."""

    def __init__(
        self, provider: ChatProvider, channel_prefix: str = "summon", bot_user_id: str = ""
    ) -> None:
        self._provider = provider
        self._prefix = channel_prefix
        self._bot_user_id = bot_user_id

    async def create_session_channel(self, session_name: str) -> tuple[str, str]:
        """Create a dedicated channel for the session.

        Returns (channel_id, channel_name).
        Handles name conflicts by appending a numeric counter.
        """
        base_name = self._make_channel_name(session_name)
        channel_id, channel_name = await self._create_with_fallback(base_name)
        logger.info("Created session channel %s (id=%s)", channel_name, channel_id)
        return channel_id, channel_name

    async def invite_user_to_channel(self, channel_id: str, user_id: str) -> None:
        """Invite a user to a session channel.

        Skips the invite if user_id matches the bot (bot already created
        the channel and is a member).
        """
        if self._bot_user_id and user_id == self._bot_user_id:
            logger.debug("Skipping invite — user %s is the bot", user_id)
            return
        try:
            await self._provider.invite_user(channel_id, user_id)
            logger.info("Invited user %s to channel %s", user_id, channel_id)
        except Exception as e:
            logger.warning("Failed to invite user %s to channel %s: %s", user_id, channel_id, e)

    async def archive_session_channel(self, channel_id: str) -> None:
        """Post a closing message and archive the channel."""
        try:
            await self._provider.post_message(
                channel_id,
                "Session ended. This channel is now archived.",
            )
            await self._provider.archive_channel(channel_id)
            logger.info("Archived channel %s", channel_id)
        except Exception as e:
            logger.warning("Failed to archive channel %s: %s", channel_id, e)

    async def post_session_header(self, channel_id: str, session_info: dict) -> str:
        """Post an initial message block with session metadata. Returns message timestamp."""
        cwd = session_info.get("cwd", "unknown")
        model = session_info.get("model") or "default"
        session_id = session_info.get("session_id", "")
        git_branch = _get_git_branch(cwd)

        fields = [
            {"type": "mrkdwn", "text": f"*Directory:*\n`{cwd}`"},
            {"type": "mrkdwn", "text": f"*Model:*\n{model}"},
        ]
        if git_branch:
            fields.append({"type": "mrkdwn", "text": f"*Branch:*\n`{git_branch}`"})
        if session_id:
            fields.append({"type": "mrkdwn", "text": f"*Session ID:*\n`{session_id[:16]}...`"})

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Claude Code Session",
                },
            },
            {"type": "section", "fields": fields},
            {"type": "divider"},
        ]

        ref = await self._provider.post_message(
            channel_id,
            f"Claude Code session started in {cwd}",
            blocks=blocks,
        )
        return ref.ts

    def format_topic(
        self,
        *,
        model: str | None,
        cwd: str,
        git_branch: str | None,
        context: ContextUsage | None,
    ) -> str:
        """Build the channel topic string with session metadata."""
        # Model: strip 'claude-' prefix for brevity
        if model and model.startswith("claude-"):
            model_short = model[len("claude-") :]
        else:
            model_short = model or "default"

        # CWD: use ~ for home directory
        try:
            cwd_display = "~/" + str(Path(cwd).relative_to(Path.home()))
        except ValueError:
            cwd_display = cwd

        # Context usage string
        if context is not None:
            ctx_k = context.input_tokens // 1000
            win_k = context.context_window // 1000
            ctx_str = f"{ctx_k}k/{win_k}k ({context.percentage:.0f}%)"
        else:
            ctx_str = "--"

        parts = [f"\U0001f916 {model_short}", f"\U0001f4c2 {cwd_display}"]
        if git_branch:
            branch_display = git_branch[:50]
            parts.append(f"\U0001f33f {branch_display}")
        parts.append(f"\U0001f4ca {ctx_str}")

        topic = " \u00b7 ".join(parts)
        return topic[:250]

    async def update_topic(self, channel_id: str, topic: str) -> None:
        """Set the channel topic via the provider."""
        try:
            await self._provider.set_topic(channel_id, topic)
        except Exception as e:
            logger.debug("Failed to set topic for channel %s: %s", channel_id, e)

    async def set_session_topic(
        self,
        channel_id: str,
        *,
        model: str | None,
        cwd: str,
        git_branch: str | None,
        context: ContextUsage | None,
    ) -> None:
        """Format and set the session topic in one call."""
        topic = self.format_topic(
            model=model,
            cwd=cwd,
            git_branch=git_branch,
            context=context,
        )
        await self.update_topic(channel_id, topic)

    def _make_channel_name(self, session_name: str) -> str:
        """Build a slugified channel name with prefix and date suffix."""
        date_suffix = datetime.now(UTC).strftime("%m%d")
        slug = _slugify(session_name) if session_name else "session"
        name = f"{self._prefix}-{slug}-{date_suffix}"
        # Slack channel names must be <= 80 chars and lowercase
        return name[:_MAX_CHANNEL_NAME_LEN].lower()

    async def _create_with_fallback(self, base_name: str) -> tuple[str, str]:
        """Try to create a channel; append counter on name collision.

        Returns (channel_id, channel_name).
        """
        name = base_name
        for attempt in range(20):
            if attempt > 0:
                suffix = f"-{attempt}"
                trimmed = base_name[: _MAX_CHANNEL_NAME_LEN - len(suffix)]
                name = f"{trimmed}{suffix}"

            try:
                ref = await self._provider.create_channel(name, is_private=True)
                return ref.channel_id, ref.name
            except Exception as e:
                err_str = str(e)
                if "name_taken" in err_str:
                    logger.debug("Channel name %r taken, trying next suffix", name)
                    continue
                raise

        raise RuntimeError(f"Could not create channel after 20 attempts: base={base_name!r}")


def _slugify(text: str) -> str:
    """Convert text to a Slack-safe channel name slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\-]", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-") or "session"


def _get_git_branch(cwd: str) -> str | None:
    """Return the current git branch for the given directory, or None if not in a repo.

    Uses GIT_CEILING_DIRECTORIES to prevent git from discovering
    repositories in parent directories above cwd.
    """
    cwd_path = Path(cwd)
    if not cwd_path.is_absolute() or not cwd_path.is_dir():
        return None
    resolved = str(cwd_path.resolve())
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    env["GIT_CEILING_DIRECTORIES"] = resolved
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=resolved,
            capture_output=True,
            text=True,
            timeout=3,
            env=env,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch != "HEAD" else None
    except Exception as e:
        logger.debug("Git branch detection failed: %s", e)
    return None
