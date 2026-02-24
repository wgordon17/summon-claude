"""Provider abstractions for chat platforms."""

from summon_claude.providers.base import ChannelRef, ChatProvider, MessageRef
from summon_claude.providers.slack import SlackChatProvider

__all__ = ["ChannelRef", "ChatProvider", "MessageRef", "SlackChatProvider"]
