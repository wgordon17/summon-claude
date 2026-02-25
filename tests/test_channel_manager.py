"""Tests for summon_claude.channel_manager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from helpers import make_mock_provider
from summon_claude.channel_manager import ChannelManager, _get_git_branch, _slugify
from summon_claude.context import ContextUsage
from summon_claude.providers.base import ChannelRef, MessageRef


class TestChannelManagerCreateChannel:
    async def test_create_returns_channel_id_and_name(self):
        provider = make_mock_provider()
        provider.create_channel = AsyncMock(
            return_value=ChannelRef(channel_id="C_TEST_123", name="summon-test-0101")
        )
        mgr = ChannelManager(provider, channel_prefix="summon")
        channel_id, channel_name = await mgr.create_session_channel("my-feature")
        assert channel_id == "C_TEST_123"
        assert channel_name == "summon-test-0101"
        provider.create_channel.assert_called_once()

    async def test_create_channel_is_private(self):
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        await mgr.create_session_channel("test")
        call_kwargs = provider.create_channel.call_args[1]
        assert call_kwargs["is_private"] is True

    async def test_channel_name_includes_prefix(self):
        provider = make_mock_provider()
        mgr = ChannelManager(provider, channel_prefix="sc")
        await mgr.create_session_channel("auth-fix")
        call_args = provider.create_channel.call_args[0]
        name = call_args[0]
        assert name.startswith("sc-")

    async def test_channel_name_is_lowercase(self):
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        await mgr.create_session_channel("MyFeature")
        call_args = provider.create_channel.call_args[0]
        name = call_args[0]
        assert name == name.lower()

    async def test_channel_name_truncated_to_80_chars(self):
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        long_name = "x" * 200
        await mgr.create_session_channel(long_name)
        call_args = provider.create_channel.call_args[0]
        name = call_args[0]
        assert len(name) <= 80

    async def test_name_collision_retries_with_counter(self):
        provider = make_mock_provider()
        # First call raises (name taken), second succeeds
        provider.create_channel = AsyncMock(
            side_effect=[
                Exception("name_taken"),
                ChannelRef(channel_id="C_RETRY_456", name="summon-test-1"),
            ]
        )
        mgr = ChannelManager(provider)
        channel_id, channel_name = await mgr.create_session_channel("existing")
        assert channel_id == "C_RETRY_456"
        assert channel_name == "summon-test-1"
        assert provider.create_channel.call_count == 2

    async def test_second_attempt_has_numeric_suffix(self):
        provider = make_mock_provider()
        provider.create_channel = AsyncMock(
            side_effect=[
                Exception("name_taken"),
                ChannelRef(channel_id="C_OK", name="summon-test-1"),
            ]
        )
        mgr = ChannelManager(provider)
        await mgr.create_session_channel("test")
        second_call_name = provider.create_channel.call_args_list[1][0][0]
        assert second_call_name.endswith("-1")

    async def test_other_api_error_raises(self):
        provider = make_mock_provider()
        provider.create_channel = AsyncMock(side_effect=Exception("not_authed"))
        mgr = ChannelManager(provider)
        with pytest.raises(Exception, match="not_authed"):
            await mgr.create_session_channel("test")


class TestChannelManagerInviteUser:
    async def test_invite_calls_provider(self):
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        await mgr.invite_user_to_channel("C_TEST", "U_USER")
        provider.invite_user.assert_called_once_with("C_TEST", "U_USER")

    async def test_invite_error_is_swallowed(self):
        provider = make_mock_provider()
        provider.invite_user = AsyncMock(side_effect=Exception("already_in_channel"))
        mgr = ChannelManager(provider)
        # Should not raise
        await mgr.invite_user_to_channel("C_TEST", "U_USER")


class TestChannelManagerArchive:
    async def test_archive_posts_message_and_archives(self):
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        await mgr.archive_session_channel("C_ARCH_123")
        provider.post_message.assert_called_once()
        provider.archive_channel.assert_called_once_with("C_ARCH_123")

    async def test_archive_error_is_swallowed(self):
        provider = make_mock_provider()
        provider.archive_channel = AsyncMock(side_effect=Exception("network error"))
        mgr = ChannelManager(provider)
        # Should not raise
        await mgr.archive_session_channel("C_ARCH_ERR")


class TestChannelManagerPostHeader:
    async def test_post_header_returns_timestamp(self):
        provider = make_mock_provider()
        provider.post_message = AsyncMock(
            return_value=MessageRef(channel_id="C_HEADER", ts="9999.0001")
        )
        mgr = ChannelManager(provider)
        ts = await mgr.post_session_header(
            "C_HEADER",
            {"cwd": "/tmp", "model": "claude-opus-4-6", "session_id": "abc123"},
        )
        assert ts == "9999.0001"

    async def test_post_header_includes_cwd_in_blocks(self):
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        await mgr.post_session_header("C_H", {"cwd": "/my/project", "session_id": "x"})
        call_kwargs = provider.post_message.call_args[1]
        blocks = call_kwargs.get("blocks", [])
        blocks_str = str(blocks)
        assert "/my/project" in blocks_str

    async def test_post_header_includes_model(self):
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        await mgr.post_session_header(
            "C_H2", {"cwd": "/tmp", "model": "claude-sonnet-4-5", "session_id": "y"}
        )
        call_kwargs = provider.post_message.call_args[1]
        blocks = call_kwargs.get("blocks", [])
        blocks_str = str(blocks)
        assert "claude-sonnet-4-5" in blocks_str


class TestMakeChannelName:
    def test_name_format(self):
        mgr = ChannelManager(make_mock_provider(), channel_prefix="summon")
        name = mgr._make_channel_name("auth-refactor")
        # Should be summon-auth-refactor-MMDD
        assert name.startswith("summon-auth-refactor-")

    def test_empty_session_name_defaults_to_session(self):
        mgr = ChannelManager(make_mock_provider())
        name = mgr._make_channel_name("")
        assert "session" in name

    def test_max_length_enforced(self):
        mgr = ChannelManager(make_mock_provider())
        name = mgr._make_channel_name("x" * 200)
        assert len(name) <= 80


class TestSlugify:
    def test_spaces_become_hyphens(self):
        assert _slugify("my feature branch") == "my-feature-branch"

    def test_uppercase_lowercased(self):
        assert _slugify("MyFeature") == "myfeature"

    def test_special_chars_replaced(self):
        # Trailing special chars get stripped along with resulting trailing hyphens
        result = _slugify("fix/auth_bug!")
        assert "fix" in result
        assert "auth" in result
        assert "bug" in result
        assert result == result.lower()

    def test_consecutive_hyphens_collapsed(self):
        assert _slugify("foo--bar") == "foo-bar"

    def test_leading_trailing_hyphens_stripped(self):
        assert _slugify("--foo--") == "foo"

    def test_empty_string_returns_session(self):
        assert _slugify("") == "session"

    def test_all_special_chars_returns_session(self):
        assert _slugify("!!!") == "session"


class TestGetGitBranch:
    def test_returns_branch_name_in_git_repo(self):
        # The test itself runs inside a git repo
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
        )
        if result.returncode == 0:
            expected = result.stdout.strip() or None
            branch = _get_git_branch(str(Path.cwd()))
            if expected and expected != "HEAD":
                assert branch == expected

    def test_returns_none_for_non_repo(self, tmp_path, monkeypatch):
        import subprocess

        original_run = subprocess.run

        def mock_run(args, **kwargs):
            # Simulate git failing in a non-repo directory
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                result = subprocess.CompletedProcess(args, returncode=128, stdout="", stderr="")
                return result
            return original_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_run)
        branch = _get_git_branch(str(tmp_path))
        assert branch is None

    def test_returns_none_for_nonexistent_dir(self):
        branch = _get_git_branch("/nonexistent/path/xyz")
        assert branch is None


class TestFormatTopic:
    def test_all_fields_present(self):
        """Topic should contain model, cwd, branch, and context emoji+values."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        context = ContextUsage(input_tokens=84000, context_window=200000, percentage=42.0)
        topic = mgr.format_topic(
            model="claude-opus-4-6",
            cwd="/home/user/projects/foo",
            git_branch="feat/bar",
            context=context,
        )
        # Check for emoji unicode chars
        assert "\U0001f916" in topic  # 🤖 robot
        assert "\U0001f4c2" in topic  # 📂 folder
        assert "\U0001f33f" in topic  # 🌿 branch
        assert "\U0001f4ca" in topic  # 📊 chart
        # Check for content
        assert "opus-4-6" in topic
        assert "feat/bar" in topic
        assert "84k/200k" in topic
        assert "42%" in topic

    def test_model_strips_claude_prefix(self):
        """Model 'claude-sonnet-4-5' should become 'sonnet-4-5' in output."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        topic = mgr.format_topic(
            model="claude-sonnet-4-5",
            cwd="/tmp",
            git_branch=None,
            context=None,
        )
        assert "sonnet-4-5" in topic
        assert "claude-sonnet-4-5" not in topic

    def test_no_git_branch_omits_segment(self):
        """When git_branch is None, no branch emoji segment should appear."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        topic = mgr.format_topic(
            model="claude-opus-4-6",
            cwd="/tmp",
            git_branch=None,
            context=None,
        )
        # Should not have branch emoji
        assert "\U0001f33f" not in topic

    def test_no_context_shows_dashes(self):
        """When context is None, context segment should show '--'."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        topic = mgr.format_topic(
            model="claude-opus-4-6",
            cwd="/tmp",
            git_branch=None,
            context=None,
        )
        assert "--" in topic

    def test_context_formats_correctly(self):
        """Context should be formatted as 'XXk/YYYk (ZZ%)'."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        context = ContextUsage(input_tokens=84000, context_window=200000, percentage=42.0)
        topic = mgr.format_topic(
            model="claude-opus-4-6",
            cwd="/tmp",
            git_branch=None,
            context=context,
        )
        assert "84k/200k" in topic
        assert "42%" in topic

    def test_truncates_to_250_chars(self):
        """Topic should be truncated to 250 characters."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        long_path = "/very/long/path/" + ("x" * 500)
        topic = mgr.format_topic(
            model="claude-opus-4-6",
            cwd=long_path,
            git_branch=None,
            context=None,
        )
        assert len(topic) <= 250

    def test_home_dir_replaced_with_tilde(self, tmp_path, monkeypatch):
        """Home directory in cwd should be replaced with ~."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        # Monkeypatch Path.home() to return a known path
        monkeypatch.setattr(Path, "home", lambda: Path(str(tmp_path)))
        home_subdir = tmp_path / "projects" / "foo"
        home_subdir.mkdir(parents=True, exist_ok=True)
        topic = mgr.format_topic(
            model="claude-opus-4-6",
            cwd=str(home_subdir),
            git_branch=None,
            context=None,
        )
        assert "~/" in topic
        assert str(tmp_path) not in topic

    def test_non_claude_model_used_as_is(self):
        """Non-Claude model should appear as-is (without 'claude-' prefix)."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        topic = mgr.format_topic(
            model="gpt-4o",
            cwd="/tmp",
            git_branch=None,
            context=None,
        )
        assert "gpt-4o" in topic

    def test_none_model_shows_default(self):
        """When model is None, should show 'default'."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        topic = mgr.format_topic(
            model=None,
            cwd="/tmp",
            git_branch=None,
            context=None,
        )
        assert "default" in topic

    def test_context_percentage_formatting(self):
        """Percentage should be formatted without decimal places (42% not 42.0%)."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        context = ContextUsage(input_tokens=100000, context_window=200000, percentage=50.0)
        topic = mgr.format_topic(
            model="claude-opus-4-6",
            cwd="/tmp",
            git_branch=None,
            context=context,
        )
        # Should have "50%" not "50.0%"
        assert "50%" in topic


class TestUpdateTopic:
    async def test_update_topic_calls_provider(self):
        """update_topic should call provider.set_topic with channel and topic."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        await mgr.update_topic("C_TEST", "test topic")
        provider.set_topic.assert_called_once_with("C_TEST", "test topic")

    async def test_update_topic_swallows_errors(self):
        """update_topic should not propagate exceptions from provider.set_topic."""
        provider = make_mock_provider()
        provider.set_topic = AsyncMock(side_effect=Exception("not_in_channel"))
        mgr = ChannelManager(provider)

        # Should not raise
        await mgr.update_topic("C_TEST", "test topic")


class TestSetSessionTopic:
    async def test_set_session_topic_formats_and_sets(self):
        """set_session_topic should format topic and call provider.set_topic."""
        provider = make_mock_provider()
        mgr = ChannelManager(provider)
        context = ContextUsage(input_tokens=84000, context_window=200000, percentage=42.0)
        await mgr.set_session_topic(
            "C_TEST",
            model="claude-opus-4-6",
            cwd="/tmp",
            git_branch="main",
            context=context,
        )
        provider.set_topic.assert_called_once()
        call_args = provider.set_topic.call_args[0]
        assert call_args[0] == "C_TEST"
        # Verify the formatted topic contains expected components
        topic = call_args[1]
        assert "opus-4-6" in topic
        assert "main" in topic
        assert "84k/200k" in topic
