"""Tests for summon_claude.mcp_untrusted_proxy."""

import asyncio
import io
import json
from unittest.mock import patch

import summon_claude.mcp_untrusted_proxy as proxy_mod
from summon_claude.mcp_untrusted_proxy import _mark_tool_result, _relay_to_parent
from summon_claude.security import UNTRUSTED_BEGIN


class TestMarkToolResult:
    def test_marks_text_content(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "email body"}]},
        }
        result = _mark_tool_result(msg, "Gmail")
        text = result["result"]["content"][0]["text"]
        assert UNTRUSTED_BEGIN in text
        assert "email body" in text
        assert "[Source: Gmail]" in text

    def test_passes_through_non_content_responses(self):
        msg = {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}
        assert _mark_tool_result(msg, "Gmail") == msg

    def test_passes_through_error_responses(self):
        msg = {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "fail"}}
        assert _mark_tool_result(msg, "Gmail") == msg

    def test_passes_through_notifications(self):
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert _mark_tool_result(msg, "Gmail") == msg

    def test_marks_multiple_text_items(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "item 1"},
                    {"type": "image", "data": "base64..."},
                    {"type": "text", "text": "item 2"},
                ]
            },
        }
        result = _mark_tool_result(msg, "Drive")
        assert UNTRUSTED_BEGIN in result["result"]["content"][0]["text"]
        assert result["result"]["content"][1] == {"type": "image", "data": "base64..."}
        assert UNTRUSTED_BEGIN in result["result"]["content"][2]["text"]

    def test_preserves_message_id(self):
        msg = {"jsonrpc": "2.0", "id": 42, "result": {"content": [{"type": "text", "text": "x"}]}}
        assert _mark_tool_result(msg, "Test")["id"] == 42

    def test_handles_none_text_value(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": None}]},
        }
        result = _mark_tool_result(msg, "Test")
        assert result["result"]["content"][0]["text"] is not None

    def test_marks_resource_content(self):
        """Resource items (e.g., Drive file contents) must be marked."""
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "resource", "resource": {"text": "file content from Drive"}}]
            },
        }
        result = _mark_tool_result(msg, "Drive")
        text = result["result"]["content"][0]["resource"]["text"]
        assert UNTRUSTED_BEGIN in text
        assert "file content from Drive" in text
        assert "[Source: Drive]" in text

    def test_truncates_long_text_content(self):
        """Text exceeding _MAX_CONTENT_CHARS is truncated before marking."""
        from summon_claude.mcp_untrusted_proxy import _MAX_CONTENT_CHARS

        long_text = "x" * (_MAX_CONTENT_CHARS + 100)
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": long_text}]},
        }
        result = _mark_tool_result(msg, "Test")
        text = result["result"]["content"][0]["text"]
        assert "[truncated by security proxy]" in text
        assert UNTRUSTED_BEGIN in text

    def test_truncates_long_resource_content(self):
        """Resource text exceeding _MAX_CONTENT_CHARS is truncated."""
        from summon_claude.mcp_untrusted_proxy import _MAX_CONTENT_CHARS

        long_text = "y" * (_MAX_CONTENT_CHARS + 50)
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "resource", "resource": {"text": long_text}}]},
        }
        result = _mark_tool_result(msg, "Test")
        text = result["result"]["content"][0]["resource"]["text"]
        assert "[truncated by security proxy]" in text
        assert UNTRUSTED_BEGIN in text

    def test_rewrites_scope_error_in_text(self):
        """Scope error needle in tool result text is replaced with CLI guidance."""
        from summon_claude.mcp_untrusted_proxy import _SCOPE_ERROR_NEEDLE

        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": f"Error: {_SCOPE_ERROR_NEEDLE}\nVisit https://..."}
                ]
            },
        }
        result = _mark_tool_result(msg, "Google Workspace")
        text = result["result"]["content"][0]["text"]
        assert _SCOPE_ERROR_NEEDLE not in text
        assert "summon auth google login" in text
        assert UNTRUSTED_BEGIN in text

    def test_passes_through_normal_text(self):
        """Text without scope error needle passes through unchanged (except marking)."""
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "normal tool output"}]},
        }
        result = _mark_tool_result(msg, "Google Workspace")
        text = result["result"]["content"][0]["text"]
        assert "normal tool output" in text
        assert "summon auth google login" not in text

    def test_scope_error_not_rewritten_in_resource(self):
        """Resource items are not rewritten — scope errors only appear in text items."""
        from summon_claude.mcp_untrusted_proxy import _SCOPE_ERROR_NEEDLE

        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "resource", "resource": {"text": f"Contains {_SCOPE_ERROR_NEEDLE}"}}
                ]
            },
        }
        result = _mark_tool_result(msg, "Drive")
        text = result["result"]["content"][0]["resource"]["text"]
        assert _SCOPE_ERROR_NEEDLE in text


class TestRelayToParent:
    """Tests for _relay_to_parent error/fallback paths."""

    async def _run_relay(self, lines: list[bytes], source: str, mark_side_effect=None) -> str:
        """Run _relay_to_parent with given input lines, capturing stdout.buffer."""
        reader = asyncio.StreamReader()
        for line in lines:
            reader.feed_data(line)
        reader.feed_eof()

        stdout_buf = io.BytesIO()

        class _FakeStdout:
            buffer = stdout_buf

        original_sys = proxy_mod.sys
        fake_sys = type("_Sys", (), {"stdout": _FakeStdout})
        proxy_mod.sys = fake_sys  # type: ignore[assignment]
        original_mark = proxy_mod._mark_tool_result
        if mark_side_effect is not None:

            def _raise(*_args: object) -> None:
                raise mark_side_effect

            proxy_mod._mark_tool_result = _raise  # type: ignore[assignment]
        try:
            await _relay_to_parent(reader, source)
        finally:
            proxy_mod.sys = original_sys  # type: ignore[assignment]
            proxy_mod._mark_tool_result = original_mark  # type: ignore[assignment]
        return stdout_buf.getvalue().decode()

    async def test_non_json_line_wrapped_as_untrusted(self):
        """Non-JSON lines must be wrapped as untrusted, not passed raw."""
        output = await self._run_relay([b"not valid json\n"], "Test")
        parsed = json.loads(output.strip())
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["id"] is None
        assert UNTRUSTED_BEGIN in parsed["result"]["content"][0]["text"]
        assert "not valid json" in parsed["result"]["content"][0]["text"]

    async def test_marking_failure_wraps_raw_with_id(self):
        """If _mark_tool_result raises, content is wrapped and id preserved."""
        valid_msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "result": {"content": [{"type": "text", "text": "data"}]},
            }
        )
        output = await self._run_relay(
            [(valid_msg + "\n").encode()],
            "Test",
            mark_side_effect=ValueError("marking failed"),
        )
        parsed = json.loads(output.strip())
        assert parsed["id"] == 42
        assert UNTRUSTED_BEGIN in parsed["result"]["content"][0]["text"]

    async def test_valid_json_passes_through_marked(self):
        """Valid JSON-RPC tool results are marked and forwarded."""
        msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "hello"}]},
            }
        )
        output = await self._run_relay([(msg + "\n").encode()], "Gmail")
        parsed = json.loads(output.strip())
        assert parsed["id"] == 1
        assert UNTRUSTED_BEGIN in parsed["result"]["content"][0]["text"]
        assert "hello" in parsed["result"]["content"][0]["text"]
