"""Tests for core ids module -- validation functions."""

from __future__ import annotations

from lattice.core.ids import (
    generate_artifact_id,
    generate_event_id,
    generate_task_id,
    validate_actor,
    validate_id,
)

# A valid 26-char Crockford Base32 ULID for reuse in tests.
_VALID_ULID = "01H0ABC0DEF000000000000000"  # 26 chars


# ---------------------------------------------------------------------------
# validate_id
# ---------------------------------------------------------------------------


class TestValidateIdHappyPath:
    """validate_id() accepts well-formed <prefix>_<ULID> strings."""

    def test_generated_task_id(self) -> None:
        tid = generate_task_id()
        assert validate_id(tid, "task") is True

    def test_generated_event_id(self) -> None:
        eid = generate_event_id()
        assert validate_id(eid, "ev") is True

    def test_generated_artifact_id(self) -> None:
        aid = generate_artifact_id()
        assert validate_id(aid, "art") is True

    def test_lowercase_ulid(self) -> None:
        assert validate_id("task_" + _VALID_ULID.lower(), "task") is True

    def test_uppercase_ulid(self) -> None:
        assert validate_id("task_" + _VALID_ULID.upper(), "task") is True

    def test_mixed_case_ulid(self) -> None:
        assert validate_id("task_" + _VALID_ULID, "task") is True


class TestValidateIdBadFormat:
    """validate_id() rejects malformed identifier strings."""

    def test_empty_string(self) -> None:
        assert validate_id("", "task") is False

    def test_no_underscore(self) -> None:
        assert validate_id("task" + _VALID_ULID, "task") is False

    def test_only_prefix(self) -> None:
        assert validate_id("task_", "task") is False

    def test_ulid_too_short(self) -> None:
        # 25 chars -- one short
        assert validate_id("task_" + "0" * 25, "task") is False

    def test_ulid_too_long(self) -> None:
        # 27 chars -- one extra
        assert validate_id("task_" + "0" * 27, "task") is False

    def test_invalid_crockford_char_I(self) -> None:
        bad = "0" * 25 + "I"  # I is excluded from Crockford Base32
        assert validate_id("task_" + bad, "task") is False

    def test_invalid_crockford_char_L(self) -> None:
        bad = "0" * 25 + "L"
        assert validate_id("task_" + bad, "task") is False

    def test_invalid_crockford_char_O(self) -> None:
        bad = "0" * 25 + "O"
        assert validate_id("task_" + bad, "task") is False

    def test_invalid_crockford_char_U(self) -> None:
        bad = "0" * 25 + "U"
        assert validate_id("task_" + bad, "task") is False

    def test_special_characters(self) -> None:
        bad = "0" * 24 + "!@"
        assert validate_id("task_" + bad, "task") is False

    def test_spaces_in_ulid(self) -> None:
        bad = "0" * 12 + " " + "0" * 13
        assert validate_id("task_" + bad, "task") is False


class TestValidateIdWrongPrefix:
    """validate_id() rejects identifiers with the wrong prefix."""

    def test_task_id_with_ev_prefix(self) -> None:
        tid = generate_task_id()
        assert validate_id(tid, "ev") is False

    def test_event_id_with_task_prefix(self) -> None:
        eid = generate_event_id()
        assert validate_id(eid, "task") is False

    def test_artifact_id_with_task_prefix(self) -> None:
        aid = generate_artifact_id()
        assert validate_id(aid, "task") is False

    def test_custom_prefix(self) -> None:
        assert validate_id("custom_" + _VALID_ULID, "custom") is True
        assert validate_id("custom_" + _VALID_ULID, "other") is False


class TestValidateIdEdgeCases:
    """validate_id() handles unusual but spec-relevant edge cases."""

    def test_non_string_input(self) -> None:
        assert validate_id(123, "task") is False  # type: ignore[arg-type]

    def test_none_input(self) -> None:
        assert validate_id(None, "task") is False  # type: ignore[arg-type]

    def test_multiple_underscores(self) -> None:
        # split(maxsplit=1) keeps everything after the first underscore,
        # so the ULID part contains an underscore and fails the regex.
        assert validate_id("task_01H00_0000000000000000000", "task") is False

    def test_prefix_with_underscore(self) -> None:
        # expected_prefix="my" but prefix part will be "my" and ulid="prefix_..."
        assert validate_id("my_prefix_" + _VALID_ULID, "my") is False


# ---------------------------------------------------------------------------
# validate_actor
# ---------------------------------------------------------------------------


class TestValidateActorHappyPath:
    """validate_actor() accepts well-formed prefix:identifier strings."""

    def test_agent_prefix(self) -> None:
        assert validate_actor("agent:claude-opus-4") is True

    def test_human_prefix(self) -> None:
        assert validate_actor("human:atin") is True

    def test_team_prefix(self) -> None:
        assert validate_actor("team:frontend") is True

    def test_identifier_with_special_chars(self) -> None:
        assert validate_actor("agent:session-abc123") is True

    def test_identifier_with_colon(self) -> None:
        # "agent:foo:bar" splits on first colon only -> identifier = "foo:bar"
        assert validate_actor("agent:foo:bar") is True


class TestValidateActorInvalidPrefix:
    """validate_actor() rejects unknown prefixes."""

    def test_unknown_prefix(self) -> None:
        assert validate_actor("bot:something") is False

    def test_system_prefix(self) -> None:
        assert validate_actor("system:root") is False

    def test_empty_prefix(self) -> None:
        assert validate_actor(":identifier") is False


class TestValidateActorBadFormat:
    """validate_actor() rejects malformed actor strings."""

    def test_no_colon(self) -> None:
        assert validate_actor("agentclaude") is False

    def test_empty_string(self) -> None:
        assert validate_actor("") is False

    def test_empty_identifier(self) -> None:
        assert validate_actor("agent:") is False

    def test_only_colon(self) -> None:
        assert validate_actor(":") is False

    def test_non_string_input(self) -> None:
        assert validate_actor(42) is False  # type: ignore[arg-type]

    def test_none_input(self) -> None:
        assert validate_actor(None) is False  # type: ignore[arg-type]
