"""AC-15 (admission and per-token limits): a stalled project starves no other; one
token cannot take more than its share; oversized bodies are refused unread."""

from __future__ import annotations

import http.client
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lattice.server import tokens
from lattice.server.testing import ServerHandle, running_server, wait_for
from tests.test_server.conftest import mint


def _timed_create(server: ServerHandle, token: str, slug: str) -> float:
    started = time.monotonic()
    status, _, body = server.op(slug, "task.create", {"title": "quick"}, token=token)
    assert status == 200, body
    return time.monotonic() - started


def test_a_stalled_project_does_not_starve_another(root: Path) -> None:
    fifty = [
        tokens.create_token(root, user=f"human:u{i}", machine="m", all_projects=True)["token"]
        for i in range(50)
    ]
    greedy, other = mint(root), mint(root, user="human:bob")
    with running_server(root) as server, ThreadPoolExecutor(70) as pool:
        slow = pool.submit(server.op, "alpha", "xtest.sleep", {"ms": 3000}, token=other)
        assert wait_for(lambda: server.project("alpha").work.locked())
        queued = [
            pool.submit(server.op, "alpha", "task.create", {"title": "q"}, token=t) for t in fifty
        ]
        greedy_queued = [
            pool.submit(server.op, "alpha", "task.create", {"title": "g"}, token=greedy)
            for _ in range(8)
        ]
        time.sleep(0.3)  # every queued request is now waiting in admission
        assert _timed_create(server, other, "beta") < 1.0
        status, headers, body = server.op("beta", "task.create", {"title": "9th"}, token=greedy)
        assert status == 429 and body["error"]["code"] == "RATE_LIMITED"
        assert headers["retry-after"] == "1"
        assert _timed_create(server, other, "beta") < 1.0
        assert slow.result()[0] == 200
        assert [f.result()[0] for f in queued] == [200] * 50
        assert [f.result()[0] for f in greedy_queued] == [200] * 8


def test_op_rate_per_token(root: Path) -> None:
    limited, other = mint(root), mint(root, user="human:bob")
    with running_server(root, config={"limits": {"token_ops_per_minute": 3}}) as server:
        for _ in range(3):
            assert server.op("alpha", "task.create", {"title": "x"}, token=limited)[0] == 200
        status, headers, body = server.op("alpha", "task.create", {"title": "x"}, token=limited)
        assert status == 429 and body["error"]["code"] == "RATE_LIMITED"
        assert int(headers["retry-after"]) >= 1
        assert server.op("alpha", "task.create", {"title": "x"}, token=other)[0] == 200


def _oversize_post(port: int, token: str, *, declared: bool) -> tuple[int, int]:
    """Start a 50 MB body, send 4 KiB of it, and read the answer. Returns (status, sent)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", "/v1/projects/alpha/ops/task.create")
    conn.putheader("Authorization", f"Bearer {token}")
    conn.putheader("Content-Type", "application/json")
    if declared:
        conn.putheader("Content-Length", str(50_000_000))
    else:
        conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    sent = 0
    try:
        for _ in range(8):
            piece = b'{"params":{"title":"' + b"x" * 490 + b'"}}'
            if declared:
                conn.send(piece)
            else:
                conn.send(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            sent += len(piece)
    except OSError:
        pass
    response = conn.getresponse()
    status = response.status
    body = json.loads(response.read())
    conn.close()
    assert body["error"]["code"] == "PAYLOAD_TOO_LARGE"
    return status, sent


def test_oversize_bodies_are_refused_unread(root: Path) -> None:
    token, other = mint(root), mint(root, user="human:bob")
    config = {"limits": {"max_body_bytes": 1024, "max_inflight_per_token": 64}}
    with running_server(root, config=config) as server, ThreadPoolExecutor(20) as pool:
        posts = [
            pool.submit(_oversize_post, server.port, token, declared=(i % 2 == 0))
            for i in range(20)
        ]
        assert _timed_create(server, other, "beta") < 1.0
        results = [p.result() for p in posts]
    assert all(status == 413 for status, _ in results)
    assert all(sent < 50_000_000 for _, sent in results)


def test_a_failed_project_is_repaired_and_reloaded_without_a_restart(root: Path) -> None:
    """AC-15 (H-22): project A unavailable (corrupt log) while B serves reads and
    writes; A's log is repaired offline; 'project reload A' serves A again."""
    import json

    from click.testing import CliRunner

    from lattice.cli.main import cli

    token = mint(root)
    with running_server(root) as server:
        status, _, body = server.op("alpha", "task.create", {"title": "a"}, token=token)
        task = body["data"]["result"]["task"]["id"]
    log = root / "projects" / "alpha" / ".lattice" / "events" / f"{task}.jsonl"
    log.write_bytes(log.read_bytes() + b'{"truncated": ')
    with running_server(root) as server:
        assert server.project("alpha").state == "unavailable"
        status, _, body = server.op("alpha", "task.create", {"title": "x"}, token=token)
        assert status == 503 and body["error"]["code"] == "BOARD_UNAVAILABLE"
        assert _timed_create(server, token, "beta") < 1.0
        status, _, _ = server.request("GET", "/v1/projects/beta/tasks", token=token)
        assert status == 200

        repaired = CliRunner().invoke(
            cli,
            ["doctor", "--fix", "--offline-maintenance", "--json"],
            env={"LATTICE_ROOT": str(root / "projects" / "alpha")},
        )
        assert repaired.exit_code == 0, repaired.output
        assert server.project("alpha").state == "unavailable"  # until reloaded
        reloaded = CliRunner().invoke(
            cli, ["server", "project", "reload", "alpha", "--root", str(root), "--json"]
        )
        assert reloaded.exit_code == 0, reloaded.output
        assert json.loads(reloaded.output)["data"]["state"] == "loaded"
        assert _timed_create(server, token, "alpha") < 1.0
        status, _, body = server.request("GET", f"/v1/projects/alpha/tasks/{task}", token=token)
        assert status == 200 and body["data"]["snapshot"]["title"] == "a"
