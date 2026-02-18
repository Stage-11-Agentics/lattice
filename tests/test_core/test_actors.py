"""Tests for core.actors — actor identity primitive."""

from __future__ import annotations

import pytest

from lattice.core.actors import (
    ActorIdentity,
    parse_disambiguated_name,
    parse_legacy_actor,
    validate_base_name,
    validate_session_creation,
)


# ---------------------------------------------------------------------------
# ActorIdentity construction and serialization
# ---------------------------------------------------------------------------


class TestActorIdentity:
    def test_basic_agent(self):
        actor = ActorIdentity(
            name="Argus-3",
            base_name="Argus",
            serial=3,
            session="sess_01ABC",
            model="claude-opus-4",
            framework="claude-code",
        )
        assert actor.name == "Argus-3"
        assert actor.base_name == "Argus"
        assert actor.serial == 3
        assert not actor.is_human
        assert actor.to_legacy_actor() == "agent:Argus"

    def test_human_actor(self):
        actor = ActorIdentity(
            name="Atin-1",
            base_name="Atin",
            serial=1,
            session="sess_01XYZ",
            model="human",
        )
        assert actor.is_human
        assert actor.to_legacy_actor() == "human:Atin"

    def test_to_dict_minimal(self):
        actor = ActorIdentity(
            name="Beacon-1",
            base_name="Beacon",
            serial=1,
            session="sess_01ABC",
            model="gpt-4.1",
            framework="codex-cli",
        )
        d = actor.to_dict()
        assert d == {
            "name": "Beacon-1",
            "base_name": "Beacon",
            "serial": 1,
            "session": "sess_01ABC",
            "model": "gpt-4.1",
            "framework": "codex-cli",
        }

    def test_to_dict_full(self):
        actor = ActorIdentity(
            name="Cipher-2",
            base_name="Cipher",
            serial=2,
            session="sess_01DEF",
            model="claude-opus-4",
            framework="claude-code",
            agent_type="advance",
            prompt="lattice-advance",
            parent="human:atin",
            extra={"custom_field": "hello"},
        )
        d = actor.to_dict()
        assert d["agent_type"] == "advance"
        assert d["prompt"] == "lattice-advance"
        assert d["parent"] == "human:atin"
        assert d["custom_field"] == "hello"

    def test_from_dict_roundtrip(self):
        original = ActorIdentity(
            name="Drift-1",
            base_name="Drift",
            serial=1,
            session="sess_01GHI",
            model="claude-opus-4",
            framework="claude-code",
            agent_type="review",
            extra={"affinity": "backend"},
        )
        d = original.to_dict()
        restored = ActorIdentity.from_dict(d)
        assert restored.name == original.name
        assert restored.serial == original.serial
        assert restored.session == original.session
        assert restored.agent_type == "review"
        assert restored.extra == {"affinity": "backend"}

    def test_from_dict_preserves_unknown_fields(self):
        d = {
            "name": "Echo-1",
            "base_name": "Echo",
            "serial": 1,
            "session": "sess_01JKL",
            "model": "claude-opus-4",
            "future_field": "future_value",
            "another": 42,
        }
        actor = ActorIdentity.from_dict(d)
        assert actor.extra == {"future_field": "future_value", "another": 42}

    def test_frozen(self):
        actor = ActorIdentity(
            name="Flint-1",
            base_name="Flint",
            serial=1,
            session="sess_01MNO",
            model="human",
        )
        with pytest.raises(AttributeError):
            actor.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidateBaseName:
    def test_valid_names(self):
        assert validate_base_name("Argus") is None
        assert validate_base_name("my-agent") is None
        assert validate_base_name("Agent_01") is None
        assert validate_base_name("X") is None

    def test_empty(self):
        assert validate_base_name("") is not None

    def test_whitespace(self):
        assert validate_base_name("has space") is not None
        assert validate_base_name("has\ttab") is not None

    def test_slashes(self):
        assert validate_base_name("path/name") is not None
        assert validate_base_name("path\\name") is not None

    def test_rejects_serial_format(self):
        """Names that look like they already have serials should be rejected."""
        assert validate_base_name("Argus-3") is not None
        assert validate_base_name("Agent-12") is not None


class TestValidateSessionCreation:
    def test_agent_valid(self):
        assert validate_session_creation(model="claude-opus-4", framework="claude-code") is None

    def test_agent_missing_framework(self):
        err = validate_session_creation(model="claude-opus-4", framework=None)
        assert err is not None
        assert "framework" in err.lower() or "Framework" in err

    def test_human_no_framework_ok(self):
        assert validate_session_creation(model="human", framework=None) is None

    def test_human_with_framework_ok(self):
        assert validate_session_creation(model="human", framework="claude-code") is None

    def test_empty_model(self):
        assert validate_session_creation(model="", framework="x") is not None


# ---------------------------------------------------------------------------
# Name parsing
# ---------------------------------------------------------------------------


class TestParseDisambiguatedName:
    def test_valid(self):
        assert parse_disambiguated_name("Argus-3") == ("Argus", 3)
        assert parse_disambiguated_name("Advance-12") == ("Advance", 12)
        assert parse_disambiguated_name("my-agent-1") == ("my-agent", 1)

    def test_no_serial(self):
        assert parse_disambiguated_name("Argus") is None
        assert parse_disambiguated_name("") is None

    def test_not_a_number(self):
        assert parse_disambiguated_name("Argus-abc") is None


class TestParseLegacyActor:
    def test_agent(self):
        result = parse_legacy_actor("agent:claude-opus-4")
        assert result["name"] == "claude-opus-4"
        assert result["model"] == "claude-opus-4"

    def test_human(self):
        result = parse_legacy_actor("human:atin")
        assert result["name"] == "atin"
        assert result["model"] == "human"

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_legacy_actor("nocolon")

    def test_empty_parts(self):
        with pytest.raises(ValueError):
            parse_legacy_actor(":identifier")
        with pytest.raises(ValueError):
            parse_legacy_actor("prefix:")
