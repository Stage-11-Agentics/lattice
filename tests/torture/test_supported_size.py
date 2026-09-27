"""AC-47 (H-10b part of the supported-size row): the initial sync and a reset of
a board at SPEC §8.8's envelope (2,000 tasks, 200 MiB of durable data, an 8 MiB
log) complete through a link throttled to 1 MiB/s under the bulk policy.

``TORTURE_ENVELOPE_MIB`` (not a ``LATTICE_*`` name: the suite strips those) shrinks
the board for a quick local run; the
default is the supported size.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lattice.remote import cache
from tests.test_remote.conftest import bind, tree_hashes
from tests.test_remote.proxies import tcp_proxy
from tests.test_remote.stub_sync_server import running_stub
from tests.torture.envelope import MIB, build_envelope

pytestmark = [pytest.mark.torture, pytest.mark.timeout(900)]


def test_initial_sync_and_reset_at_the_supported_size_through_a_slow_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    total = int(os.environ.get("TORTURE_ENVELOPE_MIB", "200")) * MIB
    server_root = tmp_path / "server"
    build_envelope(server_root / ".lattice", tasks=2000, hot_log_bytes=8 * MIB, total_bytes=total)
    with running_stub(server_root, slug="demo") as stub:
        port = int(stub.url.rsplit(":", 1)[1])
        with tcp_proxy(port, bytes_per_second=MIB) as proxy:
            client = bind(tmp_path / "client", proxy.url, stub.token, monkeypatch)

            started = time.monotonic()
            assert cache.catch_up(client, bulk=True).kind == "applied"
            first = time.monotonic() - started
            assert tree_hashes(client / ".lattice") == tree_hashes(stub.board)
            first_bytes = proxy.downstream_bytes

            stub.start_epoch()  # a rotation: the next sync is a reset
            proxy.reset_counts()
            started = time.monotonic()
            assert cache.catch_up(client, bulk=True).kind == "applied"
            reset = time.monotonic() - started
            assert tree_hashes(client / ".lattice") == tree_hashes(stub.board)
            reset_bytes = proxy.downstream_bytes

    err = capsys.readouterr().err
    assert "lattice: syncing team/demo:" in err  # progress on stderr every 5 s
    assert "locally edited" not in err
    assert first_bytes >= total  # everything crossed the throttled link once
    # The reset carried the inline part again but fetched no file the cache
    # already held byte for byte.
    assert reset_bytes < first_bytes
    print(
        f"envelope={total // MIB} MiB first_sync={first:.1f}s ({first_bytes / MIB:.1f} MiB) "
        f"reset={reset:.1f}s ({reset_bytes / MIB:.1f} MiB)"
    )
