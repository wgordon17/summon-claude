"""summon-claude: Bridge Claude Code sessions to Slack channels."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("summon-claude")
except PackageNotFoundError:
    __version__ = "unknown"
