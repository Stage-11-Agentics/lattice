"""The autouse caller-env purge (tests/conftest.py) leaves nothing from the caller's shell."""

from __future__ import annotations

import os

import pytest

from tests.conftest import purge_caller_env


def test_fixture_already_ran() -> None:
    assert not any(k.startswith(("C11_", "CMUX_")) for k in os.environ)
    assert [k for k in os.environ if k.startswith("LATTICE_")] == ["LATTICE_NO_UPDATE_CHECK"]
    assert os.environ["LATTICE_NO_UPDATE_CHECK"] == "1"


def test_caller_supplied_values_are_purged(monkeypatch: pytest.MonkeyPatch) -> None:
    caller = {
        "LATTICE_ROOT": "/some/real/board",
        "LATTICE_NO_UPDATE_CHECK": "0",
        "LATTICE_HYGIENE_DENYLIST": "/private/list",
        "C11_SURFACE_ID": "surface-uuid",
        "CMUX_SOCKET_PATH": "/tmp/sock",
    }
    for key, value in caller.items():
        monkeypatch.setenv(key, value)
    purge_caller_env(monkeypatch)
    for key in caller:
        if key == "LATTICE_NO_UPDATE_CHECK":
            assert os.environ[key] == "1", "caller-supplied value survived"
        else:
            assert key not in os.environ, key
