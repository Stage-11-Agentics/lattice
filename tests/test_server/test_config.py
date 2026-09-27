"""``server.json`` parsing and the server root (SPEC §8.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.server.admin import DEFAULT_SERVER_JSON
from lattice.server.config import (
    ServerConfig,
    ServerConfigError,
    parse_config,
    resolve_root,
)
from lattice.server.protocol import is_older, version_key


def test_defaults_match_the_spec_and_init_writes_them() -> None:
    assert parse_config({}) == ServerConfig()
    assert parse_config(DEFAULT_SERVER_JSON) == ServerConfig()


@pytest.mark.parametrize(
    "raw",
    [
        {"nope": 1},
        {"limits": {"nope": 1}},
        {"limits": {"lock_timeout_seconds": 61}},
        {"limits": {"max_body_bytes": "big"}},
        {"limits": {"max_inflight_per_token": 0}},
        {"log_level": "trace"},
        {"public_origins": "https://x"},
        {"audit": {"push": {"remote": "origin"}}},
        {"stream": {"heartbeat_seconds": 0}},
        {"port": 70000},
        [],
    ],
)
def test_invalid_config_is_refused(raw) -> None:
    with pytest.raises(ServerConfigError):
        parse_config(raw)


def test_valid_overrides() -> None:
    config = parse_config(
        {
            "limits": {"lock_timeout_seconds": 60, "max_body_bytes": 10},
            "log_level": "debug",
            "audit": {"push": {"remote": "origin", "branch": "main"}},
        }
    )
    assert config.limits.lock_timeout_seconds == 60
    assert config.limits.max_body_bytes == 10
    assert config.limits.max_inflight_per_token == 8


def test_root_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert resolve_root(None) == tmp_path / "data" / "lattice-server"
    monkeypatch.setenv("LATTICE_SERVER_ROOT", str(tmp_path / "env"))
    assert resolve_root(None) == tmp_path / "env"
    assert resolve_root(str(tmp_path / "flag")) == tmp_path / "flag"


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("0.2.1", "2.0.0"),
        ("2.0.0.dev1", "2.0.0a1"),
        ("2.0.0a1", "2.0.0rc1"),
        ("2.0.0rc1", "2.0.0"),
        ("2.0.0", "2.0.0.post1"),
        ("1.9", "1.10"),
    ],
)
def test_version_ordering(older: str, newer: str) -> None:
    assert is_older(older, newer)
    assert not is_older(newer, older)


def test_version_equality_and_garbage() -> None:
    assert version_key("2.0") == version_key("2.0.0")
    assert not is_older("garbage", "2.0.0")
    assert not is_older("2.0.0", "2.0.0")
