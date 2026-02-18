"""Tests for storage.sessions — session CRUD and serial counters."""

from __future__ import annotations

import json

import pytest

from lattice.storage.sessions import (
    create_session,
    end_session,
    list_sessions,
    resolve_session,
    touch_session,
)


@pytest.fixture()
def lattice_dir(tmp_path):
    """Create a minimal .lattice directory for session tests."""
    ld = tmp_path / ".lattice"
    ld.mkdir()
    (ld / "sessions").mkdir()
    (ld / "sessions" / "archive").mkdir()
    (ld / "locks").mkdir()
    return ld


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_basic_agent(self, lattice_dir):
        identity = create_session(
            lattice_dir,
            base_name="Argus",
            model="claude-opus-4",
            framework="claude-code",
        )
        assert identity.name == "Argus-1"
        assert identity.base_name == "Argus"
        assert identity.serial == 1
        assert identity.model == "claude-opus-4"
        assert identity.framework == "claude-code"
        assert identity.session.startswith("sess_")

    def test_serial_increments(self, lattice_dir):
        id1 = create_session(lattice_dir, base_name="Argus", model="claude-opus-4", framework="cc")
        id2 = create_session(lattice_dir, base_name="Argus", model="gpt-4.1", framework="codex")
        assert id1.serial == 1
        assert id2.serial == 2
        assert id1.name == "Argus-1"
        assert id2.name == "Argus-2"

    def test_serial_never_recycles_after_end(self, lattice_dir):
        id1 = create_session(lattice_dir, base_name="Argus", model="m", framework="f")
        end_session(lattice_dir, id1.name)
        id2 = create_session(lattice_dir, base_name="Argus", model="m", framework="f")
        assert id2.serial == 2  # Not 1

    def test_auto_generated_name(self, lattice_dir):
        identity = create_session(lattice_dir, model="claude-opus-4", framework="cc")
        assert identity.base_name  # Not empty
        assert identity.serial == 1
        assert "-" in identity.name  # Has serial

    def test_agent_type_as_name(self, lattice_dir):
        identity = create_session(
            lattice_dir,
            agent_type="advance",
            model="claude-opus-4",
            framework="cc",
        )
        assert identity.base_name == "Advance"
        assert identity.name == "Advance-1"
        assert identity.agent_type == "advance"

    def test_human_no_framework_required(self, lattice_dir):
        identity = create_session(lattice_dir, base_name="Atin", model="human")
        assert identity.name == "Atin-1"
        assert identity.is_human
        assert identity.framework is None

    def test_agent_requires_framework(self, lattice_dir):
        with pytest.raises(ValueError, match="[Ff]ramework"):
            create_session(lattice_dir, base_name="Test", model="claude-opus-4")

    def test_rejects_serial_in_name(self, lattice_dir):
        with pytest.raises(ValueError, match="serial"):
            create_session(lattice_dir, base_name="Argus-3", model="m", framework="f")

    def test_rejects_empty_name(self, lattice_dir):
        with pytest.raises(ValueError):
            create_session(lattice_dir, base_name="", model="m", framework="f")

    def test_session_file_created(self, lattice_dir):
        identity = create_session(lattice_dir, base_name="Beacon", model="m", framework="f")
        path = lattice_dir / "sessions" / f"{identity.name}.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["name"] == "Beacon-1"
        assert data["status"] == "active"
        assert "started_at" in data
        assert "last_active" in data

    def test_index_updated(self, lattice_dir):
        create_session(lattice_dir, base_name="Cipher", model="m", framework="f")
        index = json.loads((lattice_dir / "sessions" / "index.json").read_text())
        assert index["serial_counters"]["Cipher"] == 1
        assert "Cipher-1" in index["active_sessions"]

    def test_extra_fields_preserved(self, lattice_dir):
        identity = create_session(
            lattice_dir,
            base_name="Drift",
            model="m",
            framework="f",
            extra={"custom": "value"},
        )
        path = lattice_dir / "sessions" / f"{identity.name}.json"
        data = json.loads(path.read_text())
        assert data["custom"] == "value"

    def test_different_base_names_independent_serials(self, lattice_dir):
        a = create_session(lattice_dir, base_name="Alpha", model="m", framework="f")
        b = create_session(lattice_dir, base_name="Beta", model="m", framework="f")
        assert a.serial == 1
        assert b.serial == 1


# ---------------------------------------------------------------------------
# resolve_session
# ---------------------------------------------------------------------------


class TestResolveSession:
    def test_found(self, lattice_dir):
        identity = create_session(lattice_dir, base_name="Echo", model="m", framework="f")
        data = resolve_session(lattice_dir, identity.name)
        assert data is not None
        assert data["name"] == "Echo-1"

    def test_not_found(self, lattice_dir):
        assert resolve_session(lattice_dir, "Nonexistent-1") is None

    def test_not_found_after_end(self, lattice_dir):
        identity = create_session(lattice_dir, base_name="Flint", model="m", framework="f")
        end_session(lattice_dir, identity.name)
        assert resolve_session(lattice_dir, identity.name) is None


# ---------------------------------------------------------------------------
# touch_session
# ---------------------------------------------------------------------------


class TestTouchSession:
    def test_updates_last_active(self, lattice_dir):
        identity = create_session(lattice_dir, base_name="Grove", model="m", framework="f")
        original = resolve_session(lattice_dir, identity.name)
        assert touch_session(lattice_dir, identity.name)
        updated = resolve_session(lattice_dir, identity.name)
        # last_active should be updated (may or may not differ depending on speed)
        assert updated["last_active"] >= original["last_active"]

    def test_nonexistent_returns_false(self, lattice_dir):
        assert not touch_session(lattice_dir, "Ghost-1")


# ---------------------------------------------------------------------------
# end_session
# ---------------------------------------------------------------------------


class TestEndSession:
    def test_basic_end(self, lattice_dir):
        identity = create_session(lattice_dir, base_name="Helix", model="m", framework="f")
        assert end_session(lattice_dir, identity.name)
        # Session file removed
        assert not (lattice_dir / "sessions" / f"{identity.name}.json").exists()
        # Archive file created
        archives = list((lattice_dir / "sessions" / "archive").iterdir())
        assert len(archives) == 1
        archive_data = json.loads(archives[0].read_text())
        assert archive_data["status"] == "ended"
        assert "ended_at" in archive_data

    def test_end_with_reason(self, lattice_dir):
        identity = create_session(lattice_dir, base_name="Iris", model="m", framework="f")
        end_session(lattice_dir, identity.name, reason="crashed")
        archives = list((lattice_dir / "sessions" / "archive").iterdir())
        archive_data = json.loads(archives[0].read_text())
        assert archive_data["end_reason"] == "crashed"

    def test_end_removes_from_index(self, lattice_dir):
        identity = create_session(lattice_dir, base_name="Jade", model="m", framework="f")
        end_session(lattice_dir, identity.name)
        index = json.loads((lattice_dir / "sessions" / "index.json").read_text())
        assert identity.name not in index.get("active_sessions", {})
        # But counter is preserved
        assert index["serial_counters"]["Jade"] == 1

    def test_end_nonexistent(self, lattice_dir):
        assert not end_session(lattice_dir, "Nobody-1")


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_empty(self, lattice_dir):
        assert list_sessions(lattice_dir) == []

    def test_lists_active_only(self, lattice_dir):
        create_session(lattice_dir, base_name="Kite", model="m", framework="f")
        id2 = create_session(lattice_dir, base_name="Lumen", model="m", framework="f")
        end_session(lattice_dir, id2.name)
        sessions = list_sessions(lattice_dir)
        names = [s["name"] for s in sessions]
        assert "Kite-1" in names
        assert "Lumen-1" not in names

    def test_count(self, lattice_dir):
        for name in ["Mote", "Nexus", "Onyx"]:
            create_session(lattice_dir, base_name=name, model="m", framework="f")
        assert len(list_sessions(lattice_dir)) == 3
