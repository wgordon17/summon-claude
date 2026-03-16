"""Tests for summon_claude.context — context window usage tracking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from summon_claude.sessions.context import (
    ContextUsage,
    compute_context_usage,
    derive_transcript_path,
    get_last_step_usage,
)


class TestComputeContextUsage:
    def test_returns_none_when_usage_is_none(self):
        """compute_context_usage(None, model) should return None."""
        result = compute_context_usage(None, "claude-opus-4-6")
        assert result is None

    def test_returns_none_when_input_tokens_missing(self):
        """compute_context_usage without input_tokens should return None."""
        result = compute_context_usage({"output_tokens": 100}, "claude-opus-4-6")
        assert result is None

    def test_computes_total_with_cache_tokens(self):
        """Verify total includes input + cache_creation + cache_read tokens."""
        usage = {
            "input_tokens": 50000,
            "cache_creation_input_tokens": 10000,
            "cache_read_input_tokens": 5000,
        }
        result = compute_context_usage(usage, "claude-opus-4-6")
        assert result is not None
        # Total should be 65000
        assert result.input_tokens == 65000
        # Percentage = 65000/200000 * 100 = 32.5
        assert result.percentage == pytest.approx(32.5)

    def test_missing_cache_tokens_default_to_zero(self):
        """Missing cache token fields should default to 0."""
        usage = {"input_tokens": 80000}
        result = compute_context_usage(usage, "claude-opus-4-6")
        assert result is not None
        # Total should be 80000 (cache tokens missing)
        assert result.input_tokens == 80000
        assert result.percentage == pytest.approx(40.0)

    def test_known_model_prefix_matches(self):
        """Model starting with known prefix should use correct context window."""
        usage = {"input_tokens": 50000}
        # "claude-opus-4-6" matches explicit entry → 200_000 window
        result = compute_context_usage(usage, "claude-opus-4-6")
        assert result is not None
        assert result.context_window == 200_000

    def test_unknown_model_uses_default(self):
        """Unknown model should use DEFAULT_CONTEXT_WINDOW."""
        usage = {"input_tokens": 50000}
        result = compute_context_usage(usage, "gpt-4o")
        assert result is not None
        assert result.context_window == 200_000

    def test_none_model_uses_default(self):
        """model=None should use DEFAULT_CONTEXT_WINDOW."""
        usage = {"input_tokens": 50000}
        result = compute_context_usage(usage, None)
        assert result is not None
        assert result.context_window == 200_000

    def test_percentage_calculated_correctly(self):
        """84000 input / 200000 window should be 42%."""
        usage = {"input_tokens": 84000}
        result = compute_context_usage(usage, "claude-opus-4-6")
        assert result is not None
        assert result.percentage == pytest.approx(42.0)

    def test_empty_usage_dict_returns_none(self):
        """Empty dict has no input_tokens key."""
        result = compute_context_usage({}, "claude-opus-4-6")
        assert result is None

    def test_context_usage_dataclass_is_frozen(self):
        """ContextUsage should be immutable."""
        ctx = ContextUsage(input_tokens=50000, context_window=200000, percentage=25.0)
        with pytest.raises(AttributeError):
            ctx.input_tokens = 60000

    def test_context_usage_has_correct_attributes(self):
        """ContextUsage should have input_tokens, context_window, percentage."""
        ctx = ContextUsage(input_tokens=50000, context_window=200000, percentage=25.0)
        assert ctx.input_tokens == 50000
        assert ctx.context_window == 200000
        assert ctx.percentage == pytest.approx(25.0)

    def test_sonnet_model_variant(self):
        """Test with claude-sonnet model variant."""
        usage = {"input_tokens": 100000}
        result = compute_context_usage(usage, "claude-sonnet-4-5-20250514")
        assert result is not None
        assert result.context_window == 200_000

    def test_haiku_model_variant(self):
        """Test with claude-haiku model variant."""
        usage = {"input_tokens": 75000}
        result = compute_context_usage(usage, "claude-haiku-4-5")
        assert result is not None
        assert result.context_window == 200_000

    def test_percentage_precision(self):
        """Verify percentage calculation precision."""
        # 1 token / 200000 window = 0.0005%
        usage = {"input_tokens": 1}
        result = compute_context_usage(usage, "claude-opus-4-6")
        assert result is not None
        assert result.percentage == pytest.approx(0.0005)

    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4-6[1m]",
            "claude-sonnet-4-6[1m]",
            "claude-sonnet-4-5[1m]",
            "claude-sonnet-4-0[1m]",
        ],
    )
    def test_1m_model_suffix(self, model: str):
        """Models with [1m] suffix should use 1M context window."""
        usage = {"input_tokens": 500_000}
        result = compute_context_usage(usage, model)
        assert result is not None
        assert result.context_window == 1_000_000
        assert result.percentage == pytest.approx(50.0)

    def test_1m_suffix_on_non_capable_model_uses_default(self):
        """[1m] on a model not in the capable list should use 200K."""
        usage = {"input_tokens": 50_000}
        result = compute_context_usage(usage, "claude-haiku-4-5[1m]")
        assert result is not None
        assert result.context_window == 200_000

    @pytest.mark.parametrize(
        "model,expected_window",
        [
            ("claude-opus-4-6", 200_000),
            ("claude-sonnet-4-6", 200_000),
            ("claude-haiku-4-5", 200_000),
            ("claude-sonnet-4-5", 200_000),
            ("claude-opus-4-5", 200_000),
            ("claude-opus-4-1", 200_000),
            ("claude-3-7-sonnet-20250219", 200_000),
        ],
    )
    def test_newer_model_entries(self, model: str, expected_window: int):
        """All newer model variants should resolve to expected context window."""
        usage = {"input_tokens": 50_000}
        result = compute_context_usage(usage, model)
        assert result is not None
        assert result.context_window == expected_window

    def test_cache_tokens_only(self):
        """Test with cache tokens but zero input tokens."""
        usage = {
            "input_tokens": 0,
            "cache_creation_input_tokens": 5000,
            "cache_read_input_tokens": 1000,
        }
        result = compute_context_usage(usage, "claude-opus-4-6")
        assert result is not None
        assert result.input_tokens == 6000
        assert result.percentage == pytest.approx(3.0)


class TestDeriveTranscriptPath:
    def test_returns_expected_path(self):
        path = derive_transcript_path("/tmp/test", "session-123")
        assert str(path).endswith("sessions/session-123.jsonl")
        assert ".claude/projects/" in str(path)

    def test_path_hash_deterministic(self):
        p1 = derive_transcript_path("/tmp/test", "s1")
        p2 = derive_transcript_path("/tmp/test", "s1")
        assert p1 == p2

    def test_different_cwd_gives_different_hash(self):
        p1 = derive_transcript_path("/tmp/a", "s1")
        p2 = derive_transcript_path("/tmp/b", "s1")
        assert p1 != p2


class TestGetLastStepUsage:
    def test_returns_none_for_missing_file(self, tmp_path):
        result = get_last_step_usage(tmp_path / "nonexistent.jsonl")
        assert result is None

    def test_returns_last_usage(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        lines = [
            {"message": {"usage": {"input_tokens": 1000}}},
            {"message": {"usage": {"input_tokens": 2000}}},
            {"message": {"usage": {"input_tokens": 3000}}},
        ]
        transcript.write_text("\n".join(json.dumps(line) for line in lines))
        result = get_last_step_usage(transcript)
        assert result is not None
        assert result["input_tokens"] == 3000

    def test_skips_subagent_entries(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        lines = [
            {"message": {"usage": {"input_tokens": 1000}}},
            {"parentToolUseId": "tu_1", "message": {"usage": {"input_tokens": 5000}}},
            {"message": {"usage": {"input_tokens": 2000}}},
        ]
        transcript.write_text("\n".join(json.dumps(line) for line in lines))
        result = get_last_step_usage(transcript)
        assert result is not None
        assert result["input_tokens"] == 2000

    def test_handles_large_file_tail_read(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        # Write >64KB of padding followed by the actual usage
        padding = json.dumps({"data": "x" * 1000}) + "\n"
        with transcript.open("w") as f:
            for _ in range(100):
                f.write(padding)
            f.write(json.dumps({"message": {"usage": {"input_tokens": 42000}}}))
        result = get_last_step_usage(transcript)
        assert result is not None
        assert result["input_tokens"] == 42000

    def test_handles_empty_file(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        transcript.write_text("")
        result = get_last_step_usage(transcript)
        assert result is None
