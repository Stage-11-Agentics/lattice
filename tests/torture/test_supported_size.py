"""AC-47 (H-10b part of the supported-size row): the initial sync and a reset of
a board at SPEC §8.8's envelope (2,000 tasks, 200 MiB of durable data, an 8 MiB
log) complete through a link throttled to 1 MiB/s under the bulk policy. Real
server (``serve_board``), real board built through operations.

``TORTURE_ENVELOPE_MIB`` (not a ``LATTICE_*`` name: the suite strips those)
shrinks the board for a quick local run; the default is the supported size,
which takes about 5 minutes at 1 MiB/s.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lattice.remote import cache
from lattice.server.testing import serve_board
from tests.test_remote.conftest import bind, tree_hashes
from tests.test_remote.proxies import tcp_proxy
from tests.torture.envelope import MIB, STARTUP_TIMEOUT, build_envelope, durable_bytes

pytestmark = [pytest.mark.torture, pytest.mark.envelope, pytest.mark.timeout(900)]


def test_initial_sync_and_reset_at_the_supported_size_through_a_slow_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    total = int(os.environ.get("TORTURE_ENVELOPE_MIB", "200")) * MIB
    source = tmp_path / "source"
    build_envelope(source, tasks=2000, hot_log_bytes=8 * MIB, total_bytes=total)
    assert durable_bytes(source / ".lattice") >= total
    with serve_board(
        tmp_path / "server", source=source, startup_timeout=STARTUP_TIMEOUT
    ) as server:
        with tcp_proxy(server.handle.port, bytes_per_second=MIB) as proxy:
            client = bind(
                tmp_path / "client", proxy.url, server.token, monkeypatch, project=server.slug
            )

            started = time.monotonic()
            assert cache.catch_up(client, bulk=True).kind == "applied"
            first = time.monotonic() - started
            assert tree_hashes(client / ".lattice") == tree_hashes(server.board)
            first_bytes = proxy.downstream_bytes

            server.rotate_epoch()  # the next sync is a reset
            proxy.reset_counts()
            started = time.monotonic()
            assert cache.catch_up(client, bulk=True).kind == "applied"
            reset = time.monotonic() - started
            assert tree_hashes(client / ".lattice") == tree_hashes(server.board)
            reset_bytes = proxy.downstream_bytes

    err = capsys.readouterr().err
    assert "lattice: syncing team/" in err  # progress on stderr every 5 s
    assert "locally edited" not in err
    assert first_bytes >= total  # everything crossed the throttled link once
    # The reset carried the inline part again but fetched no file the cache
    # already held byte for byte.
    assert reset_bytes < first_bytes
    print(
        f"envelope={total // MIB} MiB first_sync={first:.1f}s ({first_bytes / MIB:.1f} MiB) "
        f"reset={reset:.1f}s ({reset_bytes / MIB:.1f} MiB)"
    )
