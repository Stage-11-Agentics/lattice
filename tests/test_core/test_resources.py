"""Tests for lattice.core.resources — pure logic, no filesystem."""

from __future__ import annotations

import copy

import pytest

from lattice.core.events import create_resource_event
from lattice.core.resources import (
    apply_resource_event_to_snapshot,
    compute_expires_at,
    evict_stale_holders,
    find_holder,
    format_duration_ago,
    format_duration_remaining,
    is_holder_stale,
    is_resource_available,
    serialize_resource_snapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS_BASE = "2026-02-16T10:00:00Z"
_TS_LATER = "2026-02-16T10:05:00Z"
_TS_EXPIRED = "2026-02-16T10:06:00Z"
_RES_ID = "res_01TESTRESOURCE0000000000"


def _make_created_event(
    resource_id: str = _RES_ID,
    name: str = "browser",
    max_holders: int = 1,
    ttl_seconds: int = 300,
    description: str | None = None,
    actor: str = "human:atin",
    ts: str = _TS_BASE,
) -> dict:
    data = {"name": name, "max_holders": max_holders, "ttl_seconds": ttl_seconds}
    if description:
        data["description"] = description
    return create_resource_event(
        "resource_created",
        resource_id,
        actor,
        data,
        event_id="ev_CREATE000000000000000000",
        ts=ts,
    )


def _make_snapshot(
    resource_id: str = _RES_ID,
    name: str = "browser",
    max_holders: int = 1,
    ttl_seconds: int = 300,
    holders: list[dict] | None = None,
) -> dict:
    """Build a minimal resource snapshot for testing."""
    event = _make_created_event(
        resource_id=resource_id, name=name, max_holders=max_holders, ttl_seconds=ttl_seconds
    )
    snap = apply_resource_event_to_snapshot(None, event)
    if holders:
        snap["holders"] = holders
    return snap


# ---------------------------------------------------------------------------
# Snapshot materialization
# ---------------------------------------------------------------------------


class TestApplyResourceEvent:
    """Test resource event -> snapshot materialization."""

    def test_resource_created(self) -> None:
        event = _make_created_event(description="Chrome browser")
        snap = apply_resource_event_to_snapshot(None, event)
        assert snap["id"] == _RES_ID
        assert snap["name"] == "browser"
        assert snap["max_holders"] == 1
        assert snap["ttl_seconds"] == 300
        assert snap["description"] == "Chrome browser"
        assert snap["holders"] == []
        assert snap["created_by"] == "human:atin"
        assert snap["created_at"] == _TS_BASE
        assert snap["schema_version"] == 1

    def test_resource_acquired(self) -> None:
        snap = _make_snapshot()
        event = create_resource_event(
            "resource_acquired",
            _RES_ID,
            "agent:claude",
            {"holder": "agent:claude", "expires_at": _TS_LATER},
            event_id="ev_ACQ0000000000000000000000",
            ts=_TS_BASE,
        )
        snap = apply_resource_event_to_snapshot(snap, event)
        assert len(snap["holders"]) == 1
        assert snap["holders"][0]["actor"] == "agent:claude"
        assert snap["holders"][0]["expires_at"] == _TS_LATER

    def test_resource_acquired_with_task(self) -> None:
        snap = _make_snapshot()
        event = create_resource_event(
            "resource_acquired",
            _RES_ID,
            "agent:claude",
            {"holder": "agent:claude", "expires_at": _TS_LATER, "task_id": "task_01TEST"},
            event_id="ev_ACQ0000000000000000000001",
            ts=_TS_BASE,
        )
        snap = apply_resource_event_to_snapshot(snap, event)
        assert snap["holders"][0]["task_id"] == "task_01TEST"

    def test_resource_released(self) -> None:
        snap = _make_snapshot(
            holders=[{"actor": "agent:claude", "acquired_at": _TS_BASE, "expires_at": _TS_LATER}]
        )
        event = create_resource_event(
            "resource_released",
            _RES_ID,
            "agent:claude",
            {"holder": "agent:claude"},
            event_id="ev_REL0000000000000000000000",
            ts=_TS_LATER,
        )
        snap = apply_resource_event_to_snapshot(snap, event)
        assert snap["holders"] == []

    def test_resource_heartbeat(self) -> None:
        snap = _make_snapshot(
            holders=[
                {
                    "actor": "agent:claude",
                    "acquired_at": _TS_BASE,
                    "expires_at": _TS_LATER,
                    "last_heartbeat": _TS_BASE,
                }
            ]
        )
        new_expires = "2026-02-16T10:10:00Z"
        event = create_resource_event(
            "resource_heartbeat",
            _RES_ID,
            "agent:claude",
            {"holder": "agent:claude", "expires_at": new_expires},
            event_id="ev_HB00000000000000000000000",
            ts=_TS_LATER,
        )
        snap = apply_resource_event_to_snapshot(snap, event)
        assert snap["holders"][0]["expires_at"] == new_expires
        assert snap["holders"][0]["last_heartbeat"] == _TS_LATER

    def test_resource_expired(self) -> None:
        snap = _make_snapshot(
            holders=[{"actor": "agent:claude", "acquired_at": _TS_BASE, "expires_at": _TS_LATER}]
        )
        event = create_resource_event(
            "resource_expired",
            _RES_ID,
            "agent:other",
            {"holder": "agent:claude", "expired_at": _TS_LATER, "reclaimed_by": "agent:other"},
            event_id="ev_EXP0000000000000000000000",
            ts=_TS_EXPIRED,
        )
        snap = apply_resource_event_to_snapshot(snap, event)
        assert snap["holders"] == []

    def test_resource_updated(self) -> None:
        snap = _make_snapshot()
        event = create_resource_event(
            "resource_updated",
            _RES_ID,
            "human:atin",
            {"field": "description", "old_value": None, "new_value": "Updated desc"},
            event_id="ev_UPD0000000000000000000000",
            ts=_TS_LATER,
        )
        snap = apply_resource_event_to_snapshot(snap, event)
        assert snap["description"] == "Updated desc"

    def test_requires_created_event_first(self) -> None:
        event = create_resource_event(
            "resource_acquired",
            _RES_ID,
            "agent:claude",
            {"holder": "agent:claude", "expires_at": _TS_LATER},
        )
        with pytest.raises(ValueError, match="expected 'resource_created' first"):
            apply_resource_event_to_snapshot(None, event)

    def test_deep_copy_protects_original(self) -> None:
        snap = _make_snapshot()
        original_snap = copy.deepcopy(snap)
        event = create_resource_event(
            "resource_acquired",
            _RES_ID,
            "agent:claude",
            {"holder": "agent:claude", "expires_at": _TS_LATER},
            ts=_TS_BASE,
        )
        new_snap = apply_resource_event_to_snapshot(snap, event)
        # Original should be unchanged
        assert snap["holders"] == original_snap["holders"]
        assert len(new_snap["holders"]) == 1


# ---------------------------------------------------------------------------
# TTL and availability
# ---------------------------------------------------------------------------


class TestTTL:
    """Test TTL-related functions."""

    def test_holder_not_stale_before_expiry(self) -> None:
        holder = {"actor": "agent:claude", "expires_at": _TS_LATER}
        assert is_holder_stale(holder, _TS_BASE) is False

    def test_holder_stale_after_expiry(self) -> None:
        holder = {"actor": "agent:claude", "expires_at": _TS_BASE}
        assert is_holder_stale(holder, _TS_LATER) is True

    def test_holder_stale_at_exact_expiry(self) -> None:
        holder = {"actor": "agent:claude", "expires_at": _TS_BASE}
        # At exact expiry time, string comparison: "...10:00:00Z" < "...10:00:00Z" is False
        assert is_holder_stale(holder, _TS_BASE) is False

    def test_holder_without_expires_not_stale(self) -> None:
        holder = {"actor": "agent:claude"}
        assert is_holder_stale(holder, _TS_BASE) is False

    def test_evict_stale_holders(self) -> None:
        snap = _make_snapshot(
            holders=[
                {"actor": "agent:claude", "expires_at": _TS_BASE, "acquired_at": _TS_BASE},
                {"actor": "agent:codex", "expires_at": _TS_EXPIRED, "acquired_at": _TS_BASE},
            ]
        )
        stale = evict_stale_holders(snap, _TS_LATER)
        assert len(stale) == 1
        assert stale[0]["actor"] == "agent:claude"
        assert len(snap["holders"]) == 1
        assert snap["holders"][0]["actor"] == "agent:codex"

    def test_compute_expires_at(self) -> None:
        result = compute_expires_at(300, _TS_BASE)
        assert result == _TS_LATER

    def test_compute_expires_at_longer(self) -> None:
        result = compute_expires_at(3600, _TS_BASE)
        assert result == "2026-02-16T11:00:00Z"


class TestAvailability:
    """Test resource availability checks."""

    def test_available_when_no_holders(self) -> None:
        snap = _make_snapshot()
        assert is_resource_available(snap, _TS_BASE) is True

    def test_not_available_when_held(self) -> None:
        snap = _make_snapshot(holders=[{"actor": "agent:claude", "expires_at": _TS_LATER}])
        assert is_resource_available(snap, _TS_BASE) is False

    def test_available_when_holder_expired(self) -> None:
        snap = _make_snapshot(holders=[{"actor": "agent:claude", "expires_at": _TS_BASE}])
        assert is_resource_available(snap, _TS_LATER) is True

    def test_available_multi_holder(self) -> None:
        snap = _make_snapshot(
            max_holders=2, holders=[{"actor": "agent:claude", "expires_at": _TS_LATER}]
        )
        assert is_resource_available(snap, _TS_BASE) is True

    def test_not_available_multi_holder_full(self) -> None:
        snap = _make_snapshot(
            max_holders=2,
            holders=[
                {"actor": "agent:claude", "expires_at": _TS_LATER},
                {"actor": "agent:codex", "expires_at": _TS_LATER},
            ],
        )
        assert is_resource_available(snap, _TS_BASE) is False

    def test_find_holder_exists(self) -> None:
        snap = _make_snapshot(holders=[{"actor": "agent:claude", "expires_at": _TS_LATER}])
        h = find_holder(snap, "agent:claude")
        assert h is not None
        assert h["actor"] == "agent:claude"

    def test_find_holder_not_exists(self) -> None:
        snap = _make_snapshot(holders=[{"actor": "agent:claude", "expires_at": _TS_LATER}])
        assert find_holder(snap, "agent:codex") is None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class TestFormatting:
    """Test human-readable duration formatting."""

    def test_duration_ago_seconds(self) -> None:
        result = format_duration_ago("2026-02-16T09:59:30Z", "2026-02-16T10:00:00Z")
        assert result == "30s ago"

    def test_duration_ago_minutes(self) -> None:
        result = format_duration_ago(_TS_BASE, _TS_LATER)
        assert result == "5m ago"

    def test_duration_ago_hours(self) -> None:
        result = format_duration_ago("2026-02-16T08:00:00Z", "2026-02-16T10:00:00Z")
        assert result == "2h ago"

    def test_duration_remaining_minutes(self) -> None:
        result = format_duration_remaining(_TS_LATER, _TS_BASE)
        assert result == "5m"

    def test_duration_remaining_expired(self) -> None:
        result = format_duration_remaining(_TS_BASE, _TS_LATER)
        assert result == "expired"

    def test_duration_remaining_seconds(self) -> None:
        result = format_duration_remaining("2026-02-16T10:00:30Z", _TS_BASE)
        assert result == "30s"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerializeResourceSnapshot:
    """Test resource snapshot serialization."""

    def test_produces_valid_json(self) -> None:
        snap = _make_snapshot()
        text = serialize_resource_snapshot(snap)
        import json

        parsed = json.loads(text)
        assert parsed["id"] == _RES_ID
        assert parsed["name"] == "browser"

    def test_sorted_keys(self) -> None:
        snap = _make_snapshot()
        text = serialize_resource_snapshot(snap)
        assert text.index('"created_at"') < text.index('"name"')

    def test_trailing_newline(self) -> None:
        snap = _make_snapshot()
        text = serialize_resource_snapshot(snap)
        assert text.endswith("\n")
        assert not text.endswith("\n\n")
