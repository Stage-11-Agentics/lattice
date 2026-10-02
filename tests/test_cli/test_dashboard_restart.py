"""The restart command targets only the listening dashboard process."""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lattice.cli import dashboard_cmd as dashboard_module
from lattice.cli.main import cli


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP is Unix-only")
def test_restart_signals_listener_not_connected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    def echo_client() -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                while data := connection.recv(1024):
                    connection.sendall(data)
        except OSError:
            pass

    server_thread = threading.Thread(target=echo_client, daemon=True)
    server_thread.start()
    client_code = """
import socket, sys
s = socket.create_connection(('127.0.0.1', int(sys.argv[1])))
print('connected', flush=True)
for line in sys.stdin:
    s.sendall(line.encode())
    print(s.recv(1024).decode(), end='', flush=True)
"""
    client = subprocess.Popen(
        [sys.executable, "-c", client_code, str(port)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert client.stdout is not None and client.stdin is not None
        assert client.stdout.readline().strip() == "connected"
        signalled: list[tuple[int, int]] = []
        lsof_calls: list[list[str]] = []
        expected = ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"]
        listener_pid = 999999

        def fake_lsof(args: list[str], **_kwargs: object) -> SimpleNamespace:
            lsof_calls.append(args)
            if "-sTCP:LISTEN" in args:
                # A listening socket belongs to this test process; the client
                # owns only an established connection on the same port.
                output = f"{listener_pid}\n"
            else:
                output = f"{listener_pid}\n{client.pid}\n"
            return SimpleNamespace(stdout=output)

        with monkeypatch.context() as restart_patch:
            restart_patch.setattr(dashboard_module.subprocess, "run", fake_lsof)
            restart_patch.setattr(
                dashboard_module,
                "os",
                SimpleNamespace(kill=lambda pid, sig: signalled.append((pid, sig))),
            )
            result = CliRunner().invoke(cli, ["restart", "--port", str(port)])

        assert result.exit_code == 0, result.output
        assert signalled == [(listener_pid, signal.SIGHUP)]
        assert lsof_calls == [expected]
        assert client.poll() is None
        client.stdin.write("still-connected\n")
        client.stdin.flush()
        assert client.stdout.readline().strip() == "still-connected"
        assert client.poll() is None
    finally:
        client.terminate()
        client.wait(timeout=2)
        listener.close()
        server_thread.join(timeout=1)
