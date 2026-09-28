"""G-5 (server run, H-12): the server is independent of c11.

A ``lattice server serve`` subprocess runs with a fake ``c11`` first on its
``PATH`` and every ``C11_*`` / ``CMUX_*`` variable set, ``C11_SOCKET_PATH``
naming a listening Unix socket. The hosted parity corpus is driven through it
from bound checkouts (whose own environment has no c11). The fake is never
executed, the socket never sees a connection, and every replay still matches
its golden. (The import-graph half of G-5 is ``tests/test_ops/test_import_graph.py``.)

A subprocess server is outside the default suite's hermetic rule (EVALUATION §1):
this runs in the torture lane.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lattice.server.testing import http_request, make_root, wait_for
from tests.parity.corpus import SCENARIOS
from tests.parity.hosted import (
    ParityServer,
    _mint,
    comparable,
    declared_differences,
    durable_tree,
    NOT_HOSTED,
    hosted_target,
)
from tests.parity.record import MODES, load_golden, run_scenario

pytestmark = pytest.mark.torture

REPO_ROOT = Path(__file__).resolve().parents[2]

# The server process: the corpus's frozen clock (the goldens were recorded under
# it, tests/parity/record.py), the replay's fixture operation registered
# in-process (G-11: no server change), then ``lattice server serve``.
SERVE = """
from unittest import mock

import tests.parity.fixture_op  # noqa: F401
from tests.parity.record import FROZEN_CLOCK_TARGETS, FROZEN_NOW

for target in FROZEN_CLOCK_TARGETS:
    mock.patch(target, lambda: FROZEN_NOW).start()

from lattice.cli.main import cli

cli()
"""


class SocketWatch:
    """A Unix socket at ``C11_SOCKET_PATH`` that counts every connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connections = 0
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve_with_fake_c11(tmp_path: Path) -> Iterator[tuple[ParityServer, Path, SocketWatch]]:
    root = make_root(tmp_path, config={"audit": {"enabled": False}})
    person, strict = _mint(root)
    bin_dir = tmp_path / "c11-bin"
    bin_dir.mkdir()
    ran = tmp_path / "fake-c11-ran"
    fake = bin_dir / "c11"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{ran}"\n')
    fake.chmod(0o755)
    (bin_dir / "cmux").symlink_to(fake)
    # A short path: AF_UNIX paths are limited to 104 bytes on macOS (108 on Linux),
    # and tmp_path (or macOS's $TMPDIR) is longer than that.
    sock_dir = Path(tempfile.mkdtemp(prefix="c11-", dir="/tmp"))
    sock_path = sock_dir / "c11.sock"
    assert len(os.fsencode(sock_path)) < 104, sock_path
    watch = SocketWatch(sock_path)

    env = {k: v for k, v in os.environ.items() if not k.startswith(("LATTICE_", "C11_", "CMUX_"))}
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}",
            "C11_SOCKET_PATH": str(watch.path),
            "C11_SURFACE_ID": "surface:9",
            "C11_WORKSPACE_ID": "workspace:9",
            "C11_TAB_ID": "tab:9",
            "C11_SHELL_INTEGRATION": "1",
            "CMUX_SOCKET_PATH": str(watch.path),
            "CMUX_SURFACE_ID": "surface:9",
            "CMUX_WORKSPACE_ID": "workspace:9",
            "CMUX_TAB_ID": "tab:9",
            "CMUX_SHELL_INTEGRATION": "1",
        }
    )
    port = _free_port()
    log = (tmp_path / "serve.log").open("wb")  # a pipe nobody reads would fill and stall it
    proc = subprocess.Popen(
        [sys.executable, "-c", SERVE, "server", "serve", "--root", str(root), "--port", str(port)],
        env=env,
        cwd=tmp_path,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:

        def healthy() -> bool:
            if proc.poll() is not None:
                return True
            try:
                return http_request("GET", f"{url}/healthz", timeout=1)[0] == 200
            except OSError:
                return False

        wait_for(healthy, timeout=20)
        assert proc.poll() is None, (tmp_path / "serve.log").read_text()
        yield ParityServer(root=root, url=url, token=person, strict_token=strict), ran, watch
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
        watch.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


@pytest.mark.timeout(300)
def test_a_served_corpus_never_touches_c11(tmp_path: Path) -> None:
    replayed = 0
    with serve_with_fake_c11(tmp_path) as (server, ran, watch):
        for scenario in SCENARIOS:
            if scenario.name in NOT_HOSTED:
                continue
            for mode in MODES:
                target = hosted_target(server, scenario)
                checkout = tmp_path / "checkouts" / target.slug
                capture = run_scenario(scenario, checkout, mode=mode, target=target)
                expected = comparable(load_golden(scenario.name, mode))
                actual = comparable(declared_differences(capture, binding=target.binding))
                assert actual == expected, f"{scenario.name}.{mode} drifted through serve"
                assert durable_tree(checkout / ".lattice") == durable_tree(
                    server.board(target.slug)
                )
                replayed += 1
        assert replayed == 2 * (len(SCENARIOS) - len(NOT_HOSTED))
        assert not ran.exists(), f"the server ran the fake c11: {ran.read_text()}"
        assert watch.connections == 0
