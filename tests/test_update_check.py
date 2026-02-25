"""Tests for summon_claude.update_check."""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from summon_claude.update_check import (
    UpdateInfo,
    _fetch_latest_from_pypi,
    _read_cache,
    _write_cache,
    check_for_update,
    format_update_message,
)

_URLOPEN = "summon_claude.update_check.urllib.request.urlopen"


class TestUpdateInfo:
    """Tests for the UpdateInfo NamedTuple."""

    def test_update_info_creation(self):
        """UpdateInfo can be created with current and latest versions."""
        info = UpdateInfo(current="1.0.0", latest="2.0.0")
        assert info.current == "1.0.0"
        assert info.latest == "2.0.0"

    def test_update_info_is_namedtuple(self):
        """UpdateInfo supports tuple-like access."""
        info = UpdateInfo(current="1.0.0", latest="2.0.0")
        assert info[0] == "1.0.0"
        assert info[1] == "2.0.0"


class TestFormatUpdateMessage:
    """Tests for format_update_message."""

    def test_format_message_basic(self):
        """format_update_message produces a box with version info."""
        info = UpdateInfo(current="1.0.0", latest="2.0.0")
        msg = format_update_message(info)

        assert "1.0.0 → 2.0.0" in msg
        assert "uv tool upgrade summon-claude" in msg
        assert "SUMMON_NO_UPDATE_CHECK=1" in msg
        assert "┌" in msg and "┐" in msg
        assert "└" in msg and "┘" in msg

    def test_format_message_box_width_adjusts_to_content(self):
        """format_update_message adjusts box width based on longest line."""
        info = UpdateInfo(current="1.0.0", latest="2.0.0")
        msg = format_update_message(info)
        lines = msg.split("\n")

        # All lines should have consistent width (top/bottom bar + content + borders)
        widths = [len(line) for line in lines]
        assert len(set(widths)) == 1  # All lines same width

    def test_format_message_long_versions(self):
        """format_update_message handles long version strings."""
        info = UpdateInfo(current="1.2.3rc456dev789", latest="2.3.4a000b111c222")
        msg = format_update_message(info)

        assert "1.2.3rc456dev789 → 2.3.4a000b111c222" in msg
        # Should still be properly formatted
        assert "┌" in msg and "┐" in msg

    def test_format_message_multiline_structure(self):
        """format_update_message returns proper 5-line structure."""
        info = UpdateInfo(current="1.0.0", latest="2.0.0")
        msg = format_update_message(info)
        lines = msg.split("\n")

        assert len(lines) == 5
        assert lines[0].startswith("┌")
        assert lines[0].endswith("┐")
        assert lines[1].startswith("│")
        assert lines[1].endswith("│")
        assert lines[2].startswith("│")
        assert lines[2].endswith("│")
        assert lines[3].startswith("│")
        assert lines[3].endswith("│")
        assert lines[4].startswith("└")
        assert lines[4].endswith("┘")


class TestReadCache:
    """Tests for _read_cache."""

    def test_read_cache_missing_file_returns_none(self, tmp_path):
        """_read_cache returns None when cache file doesn't exist."""
        cache_path = tmp_path / "nonexistent.json"
        result = _read_cache(cache_path)
        assert result is None

    def test_read_cache_valid_fresh_cache(self, tmp_path):
        """_read_cache returns version from fresh cache."""
        cache_path = tmp_path / "cache.json"
        now = datetime.now(UTC).isoformat()
        cache_data = {
            "latest_version": "2.0.0",
            "last_checked": now,
        }
        cache_path.write_text(json.dumps(cache_data))

        result = _read_cache(cache_path)
        assert result == "2.0.0"

    def test_read_cache_stale_cache_returns_none(self, tmp_path):
        """_read_cache returns None when cache is older than 24 hours."""
        cache_path = tmp_path / "cache.json"
        old_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        cache_data = {
            "latest_version": "2.0.0",
            "last_checked": old_time,
        }
        cache_path.write_text(json.dumps(cache_data))

        result = _read_cache(cache_path)
        assert result is None

    def test_read_cache_just_under_24_hours_is_fresh(self, tmp_path):
        """_read_cache considers cache just under 24 hours as fresh."""
        cache_path = tmp_path / "cache.json"
        almost_24h_ago = (datetime.now(UTC) - timedelta(hours=23, minutes=59)).isoformat()
        cache_data = {
            "latest_version": "2.0.0",
            "last_checked": almost_24h_ago,
        }
        cache_path.write_text(json.dumps(cache_data))

        result = _read_cache(cache_path)
        assert result == "2.0.0"

    def test_read_cache_malformed_json_returns_none(self, tmp_path):
        """_read_cache returns None on malformed JSON."""
        cache_path = tmp_path / "cache.json"
        cache_path.write_text("{invalid json")

        result = _read_cache(cache_path)
        assert result is None

    def test_read_cache_missing_fields_returns_none(self, tmp_path):
        """_read_cache returns None when required fields are missing."""
        cache_path = tmp_path / "cache.json"
        cache_path.write_text('{"latest_version": "2.0.0"}')

        result = _read_cache(cache_path)
        assert result is None

    def test_read_cache_invalid_iso_format_returns_none(self, tmp_path):
        """_read_cache returns None when last_checked is not ISO format."""
        cache_path = tmp_path / "cache.json"
        cache_data = {
            "latest_version": "2.0.0",
            "last_checked": "not-a-valid-iso-date",
        }
        cache_path.write_text(json.dumps(cache_data))

        result = _read_cache(cache_path)
        assert result is None


class TestFetchLatestFromPypi:
    """Tests for _fetch_latest_from_pypi."""

    def test_fetch_latest_valid_response(self):
        """_fetch_latest_from_pypi parses PyPI JSON and returns version."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "3.0.0"}}).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with patch(_URLOPEN, return_value=mock_response):
            result = _fetch_latest_from_pypi()

        assert result == "3.0.0"

    def test_fetch_latest_caps_read_at_64kb(self):
        """_fetch_latest_from_pypi reads max 64KB."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "3.0.0"}}).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with patch(_URLOPEN, return_value=mock_response):
            _fetch_latest_from_pypi()

        mock_response.read.assert_called_once_with(65536)

    def test_fetch_latest_timeout_returns_none(self):
        """_fetch_latest_from_pypi returns None on timeout."""
        with patch(_URLOPEN, side_effect=urllib.error.URLError("timeout")):
            result = _fetch_latest_from_pypi()

        assert result is None

    def test_fetch_latest_url_error_returns_none(self):
        """_fetch_latest_from_pypi returns None on URLError."""
        with patch(_URLOPEN, side_effect=urllib.error.URLError("404")):
            result = _fetch_latest_from_pypi()

        assert result is None

    def test_fetch_latest_malformed_json_returns_none(self):
        """_fetch_latest_from_pypi returns None on malformed JSON."""
        mock_response = MagicMock()
        mock_response.read.return_value = b"not json"
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with patch(_URLOPEN, return_value=mock_response):
            result = _fetch_latest_from_pypi()

        assert result is None

    def test_fetch_latest_missing_version_field_returns_none(self):
        """_fetch_latest_from_pypi returns None when version field is missing."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {}}).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with patch(_URLOPEN, return_value=mock_response):
            result = _fetch_latest_from_pypi()

        assert result is None

    def test_fetch_latest_network_exception_returns_none(self):
        """_fetch_latest_from_pypi returns None on generic Exception."""
        with patch(_URLOPEN, side_effect=Exception("network error")):
            result = _fetch_latest_from_pypi()

        assert result is None


class TestWriteCache:
    """Tests for _write_cache."""

    def test_write_cache_creates_file(self, tmp_path):
        """_write_cache creates cache file with version and timestamp."""
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "2.0.0")

        assert cache_path.exists()
        data = json.loads(cache_path.read_text())
        assert data["latest_version"] == "2.0.0"
        assert "last_checked" in data

    def test_write_cache_timestamp_is_iso_format(self, tmp_path):
        """_write_cache writes timestamp in ISO format."""
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "2.0.0")

        data = json.loads(cache_path.read_text())
        # Should not raise on parsing
        datetime.fromisoformat(data["last_checked"])

    def test_write_cache_creates_parent_directories(self, tmp_path):
        """_write_cache creates missing parent directories."""
        cache_path = tmp_path / "dir1" / "dir2" / "cache.json"
        _write_cache(cache_path, "2.0.0")

        assert cache_path.exists()

    def test_write_cache_symlink_protection(self, tmp_path):
        """_write_cache refuses to write through symlinks."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        cache_path = tmp_path / "link"
        cache_path.symlink_to(real_dir / "cache.json")

        _write_cache(cache_path, "2.0.0")

        # File should not be created because symlink was refused
        assert not (real_dir / "cache.json").exists()

    def test_write_cache_handles_permission_error(self, tmp_path):
        """_write_cache silently ignores permission errors."""
        cache_path = tmp_path / "cache.json"
        # On some systems, can't make parent read-only; patch write_text instead
        with patch.object(Path, "write_text", side_effect=PermissionError("denied")):
            # Should not raise
            _write_cache(cache_path, "2.0.0")

    def test_write_cache_handles_mkdir_failure(self, tmp_path):
        """_write_cache silently ignores mkdir failures."""
        cache_path = tmp_path / "cache.json"
        with patch.object(Path, "mkdir", side_effect=OSError("cannot create")):
            # Should not raise
            _write_cache(cache_path, "2.0.0")


class TestCheckForUpdate:
    """Tests for check_for_update."""

    def test_check_for_update_no_env_var_queries_pypi(self, tmp_path, monkeypatch):
        """check_for_update queries PyPI when cache is missing."""
        monkeypatch.setenv("SUMMON_NO_UPDATE_CHECK", "0")
        monkeypatch.setattr(
            "summon_claude.update_check.get_update_check_path", lambda: tmp_path / "cache.json"
        )

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "2.0.0"}}).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with (
            patch(_URLOPEN, return_value=mock_response),
            patch("summon_claude.update_check.version", return_value="1.0.0"),
        ):
            result = check_for_update()

        assert result == UpdateInfo(current="1.0.0", latest="2.0.0")

    def test_check_for_update_no_update_env_returns_none(self, monkeypatch):
        """check_for_update returns None when SUMMON_NO_UPDATE_CHECK=1."""
        monkeypatch.setenv("SUMMON_NO_UPDATE_CHECK", "1")

        with patch(_URLOPEN) as mock_urlopen:
            result = check_for_update()

        assert result is None
        mock_urlopen.assert_not_called()

    def test_check_for_update_reads_fresh_cache(self, tmp_path, monkeypatch):
        """check_for_update reads fresh cache without querying PyPI."""
        cache_path = tmp_path / "cache.json"
        now = datetime.now(UTC).isoformat()
        cache_data = {
            "latest_version": "2.0.0",
            "last_checked": now,
        }
        cache_path.write_text(json.dumps(cache_data))

        monkeypatch.setattr("summon_claude.update_check.get_update_check_path", lambda: cache_path)

        with (
            patch(_URLOPEN) as mock_urlopen,
            patch("summon_claude.update_check.version", return_value="1.0.0"),
        ):
            result = check_for_update()

        assert result == UpdateInfo(current="1.0.0", latest="2.0.0")
        mock_urlopen.assert_not_called()

    def test_check_for_update_refetches_stale_cache(self, tmp_path, monkeypatch):
        """check_for_update queries PyPI when cache is stale."""
        cache_path = tmp_path / "cache.json"
        old_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        cache_data = {
            "latest_version": "1.5.0",
            "last_checked": old_time,
        }
        cache_path.write_text(json.dumps(cache_data))

        monkeypatch.setattr("summon_claude.update_check.get_update_check_path", lambda: cache_path)

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "2.0.0"}}).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with (
            patch(_URLOPEN, return_value=mock_response),
            patch("summon_claude.update_check.version", return_value="1.0.0"),
        ):
            result = check_for_update()

        assert result == UpdateInfo(current="1.0.0", latest="2.0.0")

    def test_check_for_update_current_equals_latest_returns_none(self, tmp_path, monkeypatch):
        """check_for_update returns None when current == latest."""
        monkeypatch.setattr(
            "summon_claude.update_check.get_update_check_path", lambda: tmp_path / "cache.json"
        )

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "1.0.0"}}).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with (
            patch(_URLOPEN, return_value=mock_response),
            patch("summon_claude.update_check.version", return_value="1.0.0"),
        ):
            result = check_for_update()

        assert result is None

    def test_check_for_update_current_greater_than_latest_returns_none(self, tmp_path, monkeypatch):
        """check_for_update returns None when current > latest (dev build)."""
        monkeypatch.setattr(
            "summon_claude.update_check.get_update_check_path", lambda: tmp_path / "cache.json"
        )

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "1.0.0"}}).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with (
            patch(_URLOPEN, return_value=mock_response),
            patch("summon_claude.update_check.version", return_value="2.0.0dev"),
        ):
            result = check_for_update()

        assert result is None

    def test_check_for_update_pypi_fetch_returns_none(self, tmp_path, monkeypatch):
        """check_for_update returns None when PyPI fetch fails."""
        monkeypatch.setattr(
            "summon_claude.update_check.get_update_check_path", lambda: tmp_path / "cache.json"
        )

        with patch(_URLOPEN, side_effect=urllib.error.URLError("timeout")):
            result = check_for_update()

        assert result is None

    def test_check_for_update_cache_write_failure_still_returns_info(self, tmp_path, monkeypatch):
        """check_for_update returns info even if cache write fails."""
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("summon_claude.update_check.get_update_check_path", lambda: cache_path)

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "2.0.0"}}).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with (
            patch(_URLOPEN, return_value=mock_response),
            patch("summon_claude.update_check.version", return_value="1.0.0"),
            patch.object(Path, "write_text", side_effect=PermissionError("denied")),
        ):
            result = check_for_update()

        assert result == UpdateInfo(current="1.0.0", latest="2.0.0")

    def test_check_for_update_generic_exception_returns_none(self, monkeypatch):
        """check_for_update returns None on any unexpected exception."""

        def raise_error():
            raise RuntimeError("something went wrong")

        monkeypatch.setattr("summon_claude.update_check.get_update_check_path", raise_error)

        result = check_for_update()

        assert result is None

    def test_check_for_update_version_parsing(self, tmp_path, monkeypatch):
        """check_for_update correctly handles version comparison."""
        monkeypatch.setattr(
            "summon_claude.update_check.get_update_check_path", lambda: tmp_path / "cache.json"
        )

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "2.0.0a1"}}).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with (
            patch(_URLOPEN, return_value=mock_response),
            patch("summon_claude.update_check.version", return_value="1.0.0"),
        ):
            result = check_for_update()

        assert result == UpdateInfo(current="1.0.0", latest="2.0.0a1")

    def test_check_for_update_caches_result(self, tmp_path, monkeypatch):
        """check_for_update writes fetched version to cache."""
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("summon_claude.update_check.get_update_check_path", lambda: cache_path)

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "2.0.0"}}).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)

        with (
            patch(_URLOPEN, return_value=mock_response),
            patch("summon_claude.update_check.version", return_value="1.0.0"),
        ):
            check_for_update()

        # Verify cache was written
        assert cache_path.exists()
        data = json.loads(cache_path.read_text())
        assert data["latest_version"] == "2.0.0"
