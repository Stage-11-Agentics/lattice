"""Fixtures of the hosted replay (``test_hosted_parity*.py``)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.parity.hosted import ParityServer, parity_server


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ParityServer]:
    """One server per worker (each xdist worker is its own session)."""
    with parity_server(tmp_path_factory.mktemp("parity-server")) as handle:
        yield handle
