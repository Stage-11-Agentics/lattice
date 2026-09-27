"""Fixtures of the hosted replay (``test_hosted_parity*.py``)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.parity.hosted import ParityServer, parity_server


@pytest.fixture()
def no_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parity is about outputs and boards, not durability (AC-4 and the transaction
    tests own that), and fsync is about a quarter of a replay: skip it while each test
    runs. The server shares this process, so its writes skip it too."""
    monkeypatch.setattr(os, "fsync", lambda fd: None)


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ParityServer]:
    """One server per worker (each xdist worker is its own session)."""
    with parity_server(tmp_path_factory.mktemp("parity-server")) as handle:
        yield handle
