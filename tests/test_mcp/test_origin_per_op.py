"""AC-36: one MCP server process, several checkouts, each event its own origin.

A real ``lattice-mcp`` process on stdio serves every call below. Each call's
``lattice_root`` is its operation's starting directory (SPEC §4, §12), so an
event names the worktree it was made from and the branch checked out there
at that moment, not the server's cwd or the branch it saw first.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lattice.core.config import default_config, serialize_config
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs

ACTOR = "agent:mcp-origin"
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True)


def _checkout(path: Path, code: str, branch: str) -> Path:
    """A git checkout on *branch* with its own local board."""
    path.mkdir()
    _git(path, "init", "-q", "-b", branch)
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    ensure_lattice_dirs(path)
    config = default_config()
    config["project_code"] = code
    atomic_write(path / LATTICE_DIR / "config.json", serialize_config(config))
    return path


class McpProcess:
    """A ``lattice-mcp`` subprocess driven over newline-delimited JSON-RPC."""

    def __init__(self, cwd: Path, extra_env: dict[str, str] | None = None) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LATTICE_")}
        env.update(extra_env or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "from lattice.mcp.server import main; main()"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._next_id = 0
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "lattice-tests", "version": "0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            assert line, "MCP server closed its stdout"
            message = json.loads(line)
            if message.get("id") == self._next_id:
                assert "error" not in message, message
                return message["result"]

    def call(self, tool: str, **arguments: object) -> dict:
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        text = result["content"][0]["text"]
        assert not result.get("isError"), text
        return json.loads(text)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


@pytest.fixture()
def mcp_server(tmp_path: Path):
    cwd = tmp_path / "server-cwd"
    cwd.mkdir()
    server = McpProcess(cwd)
    try:
        yield server
    finally:
        server.close()


def _origins(checkout: Path, task_id: str) -> list[dict]:
    path = checkout / LATTICE_DIR / "events" / f"{task_id}.jsonl"
    return [json.loads(line)["origin"] for line in path.read_text().splitlines()]


def test_one_process_two_checkouts_and_a_branch_switch(
    tmp_path: Path, mcp_server: McpProcess
) -> None:
    alpha = _checkout(tmp_path / "alpha", "ALP", "main")
    beta = _checkout(tmp_path / "beta", "BET", "trunk")

    a_task = mcp_server.call("lattice_create", title="A", actor=ACTOR, lattice_root=str(alpha))
    b_task = mcp_server.call("lattice_create", title="B", actor=ACTOR, lattice_root=str(beta))
    _git(alpha, "checkout", "-q", "-b", "feat/ALP-1-switch")
    mcp_server.call(
        "lattice_comment", task_id="ALP-1", text="after", actor=ACTOR, lattice_root=str(alpha)
    )
    mcp_server.call(
        "lattice_comment", task_id="BET-1", text="still", actor=ACTOR, lattice_root=str(beta)
    )

    a_origins = _origins(alpha, a_task["id"])
    b_origins = _origins(beta, b_task["id"])
    assert [(o["op"], o["reported"]["worktree"], o["reported"]["branch"]) for o in a_origins] == [
        ("task.create", str(alpha.resolve()), "main"),
        ("task.comment", str(alpha.resolve()), "feat/ALP-1-switch"),
    ]
    assert [(o["op"], o["reported"]["worktree"], o["reported"]["branch"]) for o in b_origins] == [
        ("task.create", str(beta.resolve()), "trunk"),
        ("task.comment", str(beta.resolve()), "trunk"),
    ]
    # One process wrote all four: the per-process fields agree, the op IDs do not.
    every = a_origins + b_origins
    assert len({o["reported"]["host"] for o in every}) == 1
    assert len({o["op_id"] for o in every}) == 4
    # The server's own cwd is in no checkout and appears nowhere.
    assert all("server-cwd" not in o["reported"]["worktree"] for o in every)


def test_linked_worktree_writes_the_primary_board_and_names_itself(
    tmp_path: Path, mcp_server: McpProcess
) -> None:
    primary = _checkout(tmp_path / "primary", "PRI", "main")
    linked = tmp_path / "linked"
    _git(primary, "worktree", "add", "-q", "-b", "feat/PRI-1-wt", str(linked))

    task = mcp_server.call("lattice_create", title="P", actor=ACTOR, lattice_root=str(primary))
    mcp_server.call(
        "lattice_comment", task_id="PRI-1", text="wt", actor=ACTOR, lattice_root=str(linked)
    )

    origins = _origins(primary, task["id"])
    assert [(o["reported"]["worktree"], o["reported"]["branch"]) for o in origins] == [
        (str(primary.resolve()), "main"),
        (str(linked.resolve()), "feat/PRI-1-wt"),
    ]
    assert not (linked / LATTICE_DIR).exists()


def test_one_process_local_and_bound_checkouts(tmp_path: Path) -> None:
    """The same, with one checkout bound to a server (H-11): the server's event
    names the bound checkout's worktree and the branch current at each write."""
    from lattice.server import tokens
    from lattice.server.testing import make_root, running_server

    local = _checkout(tmp_path / "local", "LOC", "main")
    bound = tmp_path / "bound"
    bound.mkdir()
    _git(bound, "init", "-q", "-b", "main")
    (bound / ".lattice-remote.json").write_text('{"remote": "team", "project": "alpha"}\n')
    _git(bound, "add", ".lattice-remote.json")
    _git(bound, "commit", "-q", "-m", "bind")
    root = make_root(
        tmp_path / "server",
        projects={"alpha": {"code": "ALP"}},
        config={"audit": {"enabled": False}},
    )
    token = tokens.create_token(root, user="human:alice", machine="laptop", all_projects=True)
    cwd = tmp_path / "server-cwd"
    cwd.mkdir()

    with running_server(root) as server:
        mcp = McpProcess(
            cwd,
            {"LATTICE_REMOTE_TEAM_URL": server.url, "LATTICE_REMOTE_TEAM_TOKEN": token["token"]},
        )
        try:
            hosted = mcp.call("lattice_create", title="H", actor=ACTOR, lattice_root=str(bound))
            mcp.call("lattice_create", title="L", actor=ACTOR, lattice_root=str(local))
            _git(bound, "checkout", "-q", "-b", "feat/ALP-1-switch")
            mcp.call(
                "lattice_comment", task_id="ALP-1", text="x", actor=ACTOR, lattice_root=str(bound)
            )
            shown = mcp.call("lattice_show", task_id="ALP-1", lattice_root=str(bound))
        finally:
            mcp.close()

    server_log = root / "projects" / "alpha" / LATTICE_DIR / "events" / f"{hosted['id']}.jsonl"
    origins = [json.loads(line)["origin"] for line in server_log.read_text().splitlines()]
    assert [(o["op"], o["reported"]["worktree"], o["reported"]["branch"]) for o in origins] == [
        ("task.create", str(bound.resolve()), "main"),
        ("task.comment", str(bound.resolve()), "feat/ALP-1-switch"),
    ]
    assert all(o["authenticated"]["user"] == "human:alice" for o in origins)
    assert shown["events"][-1]["type"] == "comment_added"
    local_origin = _origins(local, _only_task(local))[0]
    assert (local_origin["reported"]["worktree"], local_origin["reported"]["branch"]) == (
        str(local.resolve()),
        "main",
    )


def _only_task(checkout: Path) -> str:
    (path,) = (checkout / LATTICE_DIR / "events").glob("task_*.jsonl")
    return path.stem
