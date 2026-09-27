"""Process-level harness for the torture suite (H-15): a real ``lattice server serve``
subprocess, tokens, per-client environments, and the CLI as a subprocess.

Everything binds ``127.0.0.1`` and lives under ``tmp_path``. A client is a set of
environment variables (its own ``XDG_CONFIG_HOME`` with ``remotes.json``, its token
in ``TORTURE_TOKEN``, and optionally a reported host), so several "machines" can
run side by side on one box::

    server = ServerProcess(make_root(tmp_path, projects={"demo": {"code": "DEM"}}))
    server.start()
    alice = server.client(tmp_path / "alice", user="human:alice", machine="laptop")
    lattice(alice, repo, "create", "A task", "--actor", "agent:a")
    server.kill()      # SIGKILL
    server.start()     # a new port; every client's remotes.json is rewritten

The server binds ``127.0.0.1:0`` itself: :data:`SERVE_SHIM` runs the real
``lattice server serve --port 0`` and, once uvicorn listens, writes the bound port
to ``TORTURE_READY_FILE``, so no port is ever chosen by one process and bound by
another. Every child process and server started here is registered, and the
``torture`` conftest reaps whatever a test leaves running, pass or fail.

``TORTURE_HOST`` (not a ``LATTICE_*`` name: the suite strips those) replaces
``socket.gethostname()`` in the CLI subprocess, so one box reports several hosts.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lattice.server import tokens
from lattice.server.testing import http_request

PROJECT = "demo"
REMOTE = "team"
TOKEN_ENV = "TORTURE_TOKEN"

#: The ``lattice`` entry point, with ``TORTURE_HOST`` as the reported host.
CLI_SHIM = (
    "import os, socket, sys\n"
    "host = os.environ.get('TORTURE_HOST')\n"
    "if host:\n"
    "    socket.gethostname = lambda: host\n"
    "from lattice.cli.main import cli\n"
    "cli(prog_name='lattice')\n"
)

#: ``lattice server serve`` unchanged, except that its uvicorn server reports the
#: port it bound (``--port 0``) to ``TORTURE_READY_FILE`` once it listens.
SERVE_SHIM = (
    "import os\n"
    "from lattice.server import serve as _serve\n"
    "_server_class = _serve._server_class\n"
    "def _reporting(state):\n"
    "    base = _server_class(state)\n"
    "    class Reporting(base):\n"
    "        async def startup(self, sockets=None):\n"
    "            await super().startup(sockets)\n"
    "            port = self.servers[0].sockets[0].getsockname()[1]\n"
    "            path = os.environ['TORTURE_READY_FILE']\n"
    "            with open(path + '.tmp', 'w') as fh:\n"
    "                fh.write(str(port))\n"
    "            os.replace(path + '.tmp', path)\n"
    "    return Reporting\n"
    "_serve._server_class = _reporting\n"
    "from lattice.cli.main import cli\n"
    "cli(prog_name='lattice')\n"
)

#: Every child process and server the harness started, for the conftest's reaper.
CHILDREN: list[subprocess.Popen] = []
SERVERS: list[ServerProcess] = []


def track(proc: subprocess.Popen) -> subprocess.Popen:
    CHILDREN.append(proc)
    return proc


def reap_all() -> None:
    """Stop every server and kill every child still running (unconditional cleanup)."""
    while SERVERS:
        server = SERVERS.pop()
        try:
            server.stop()
        except Exception:  # noqa: BLE001 - cleanup keeps going; kill() below
            server.kill()
    while CHILDREN:
        proc = CHILDREN.pop()
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass


def base_env() -> dict[str, str]:
    """The test process's environment without any ``LATTICE_*`` variable."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LATTICE_")}
    env["LATTICE_NO_UPDATE_CHECK"] = "1"
    return env


@dataclass
class Client:
    """One client "machine": its environment and identity."""

    name: str
    env: dict[str, str]
    user: str
    machine: str
    host: str | None
    token: str
    remotes: Path | None = None


@dataclass
class ServerProcess:
    """``lattice server serve`` as a subprocess on a port it binds itself."""

    root: Path
    port: int = 0
    proc: subprocess.Popen | None = None
    log_path: Path | None = None
    starts: int = 0
    #: Clients whose remote points straight at this server: rewritten on restart.
    clients: list[Client] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 30.0) -> None:
        assert self.proc is None or self.proc.poll() is not None, "already running"
        if self not in SERVERS:
            SERVERS.append(self)
        self.starts += 1
        self.log_path = self.root.parent / f"server-{self.starts}.log"
        ready = self.root.parent / f"server-{self.starts}.port"
        ready.unlink(missing_ok=True)
        env = base_env()
        env["TORTURE_READY_FILE"] = str(ready)
        with open(self.log_path, "wb") as log:
            self.proc = track(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        SERVE_SHIM,
                        "server",
                        "serve",
                        "--root",
                        str(self.root),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "0",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
            )
        try:
            deadline = time.monotonic() + timeout
            while not ready.exists():
                if self.proc.poll() is not None:
                    raise AssertionError(f"server exited {self.proc.returncode}: {self.log()}")
                if time.monotonic() > deadline:
                    raise AssertionError(f"server did not listen within {timeout}s: {self.log()}")
                time.sleep(0.02)
            old_url, self.port = self.url, int(ready.read_text())
            status = http_request("GET", self.url + "/healthz", timeout=10)[0]
            assert status == 200, f"healthz answered {status}: {self.log()}"
        except BaseException:
            self.kill()
            raise
        if old_url != self.url:
            for client in self.clients:
                _point_remote(client, self.url)

    def log(self) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        return self.log_path.read_text(errors="replace")[-4000:]

    def kill(self) -> None:
        """SIGKILL: no shutdown path runs."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=10)

    def stop(self) -> None:
        """SIGTERM, the graceful path (SPEC §8.11); SIGKILL if it hangs."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=40)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
                raise AssertionError("server ignored SIGTERM for 40 s") from None

    def mint(
        self, *, user: str, machine: str, actors: tuple[str, ...] = (), project: str = PROJECT
    ) -> str:
        return tokens.create_token(
            self.root, user=user, machine=machine, actors=actors, projects=[project]
        )["token"]

    def client(
        self,
        home: Path,
        *,
        user: str = "human:alice",
        machine: str = "laptop",
        host: str | None = None,
        actors: tuple[str, ...] = (),
        token: str | None = None,
        url: str | None = None,
        headers: dict[str, tuple[str, str]] | None = None,
        retry_seconds: float = 5,
    ) -> Client:
        """A client environment under *home* with remote ``team`` pointing at this
        server (or at *url*, a proxy in front of it). *headers* maps a header name
        to ``(env var, value)``: the remote sends it from that variable."""
        token = token or self.mint(user=user, machine=machine, actors=actors)
        config = home / "config"
        remotes = config / "lattice" / "remotes.json"
        remotes.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "url": url or self.url,
            "token": {"env": TOKEN_ENV},
            "retry_seconds": retry_seconds,
        }
        env = base_env()
        if headers:
            entry["headers"] = {name: {"env": var} for name, (var, _) in headers.items()}
            env.update({var: value for var, value in headers.values()})
        remotes.write_text(json.dumps({"remotes": {REMOTE: entry}}, indent=2) + "\n")
        remotes.chmod(0o600)
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(config),
                "XDG_DATA_HOME": str(home / "data"),
                "XDG_STATE_HOME": str(home / "state"),
                "XDG_CACHE_HOME": str(home / "cache"),
                TOKEN_ENV: token,
            }
        )
        if host:
            env["TORTURE_HOST"] = host
        client = Client(
            name=home.name,
            env=env,
            user=user,
            machine=machine,
            host=host,
            token=token,
            remotes=remotes,
        )
        if url is None:
            self.clients.append(client)
        return client

    def op(
        self, op: str, params: dict, *, token: str, project: str = PROJECT, **envelope: Any
    ) -> tuple[int, Any]:
        status, _, body = http_request(
            "POST",
            f"{self.url}/v1/projects/{project}/ops/{op}",
            token=token,
            body={"params": params, **envelope},
            timeout=30,
        )
        return status, body

    def op_status(self, op_id: str, *, token: str, project: str = PROJECT) -> dict:
        status, _, body = http_request(
            "GET", f"{self.url}/v1/projects/{project}/ops/{op_id}", token=token, timeout=30
        )
        assert status == 200, body
        return body["data"]

    def board(self, project: str = PROJECT) -> Path:
        return self.root / "projects" / project / ".lattice"


def _point_remote(client: Client, url: str) -> None:
    assert client.remotes is not None
    data = json.loads(client.remotes.read_text())
    data["remotes"][REMOTE]["url"] = url
    client.remotes.write_text(json.dumps(data, indent=2) + "\n")


def lattice(
    client: Client,
    cwd: Path,
    *args: str,
    check: bool = True,
    timeout: float = 120,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    """``lattice <args>`` as a subprocess in *cwd* for *client* (killed on timeout)."""
    proc = subprocess.run(
        [sys.executable, "-c", CLI_SHIM, *args],
        cwd=cwd,
        env=client.env,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check:
        assert proc.returncode == 0, f"lattice {' '.join(args)}: {proc.stdout}{proc.stderr}"
    return proc


def lattice_json(client: Client, cwd: Path, *args: str) -> Any:
    proc = lattice(client, cwd, *args, "--json")
    return json.loads(proc.stdout)["data"]


def spawn_lattice(client: Client, cwd: Path, *args: str, log: Path) -> subprocess.Popen:
    """A long-running ``lattice`` subprocess (``sync --follow``), output to *log*."""
    with open(log, "wb") as fh:
        return track(
            subprocess.Popen(
                [sys.executable, "-c", CLI_SHIM, *args],
                cwd=cwd,
                env=client.env,
                stdout=fh,
                stderr=subprocess.STDOUT,
            )
        )


def stop_process(proc: subprocess.Popen, timeout: float = 15) -> int:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return proc.returncode


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, env=_git_env()
    )
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stdout}{proc.stderr}"
    return proc.stdout.strip()


def _git_env() -> dict[str, str]:
    env = base_env()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Torture",
            "GIT_AUTHOR_EMAIL": "torture@example.com",
            "GIT_COMMITTER_NAME": "Torture",
            "GIT_COMMITTER_EMAIL": "torture@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def board_events(board: Path) -> list[dict]:
    """Every task event on a board, active and archived."""
    found: list[dict] = []
    for rel in ("events", "archive/events"):
        directory = board / rel
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("task_*.jsonl")):
            for line in path.read_text().splitlines():
                if line.strip():
                    found.append(json.loads(line))
    return found


def chmod_tree_writable(root: Path) -> None:
    """Undo a cache's read-only modes so pytest can delete ``tmp_path``."""
    for dirpath, dirnames, _ in os.walk(root):
        for name in dirnames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                try:
                    path.chmod(0o700)
                except OSError:
                    pass


def make_repo(path: Path) -> Path:
    """A git repository with one commit on ``main``."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("repo\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "init")
    return path


def bound_checkout(client: Client, path: Path, project: str = PROJECT) -> Path:
    """A new repository at *path*, attached to *project* and the binding committed."""
    make_repo(path)
    lattice(client, path, "remote", "attach", REMOTE, project)
    git(path, "add", ".lattice-remote.json", ".gitignore")
    git(path, "commit", "-q", "-m", "bind the board")
    return path


def make_ready(client: Client, cwd: Path, task: str, actor: str = "agent:setup") -> None:
    """Write *task*'s plan and move it to ``planned`` (no auto plan review)."""
    lattice(
        client,
        cwd,
        "plan",
        "write",
        task,
        "--stdin",
        "--actor",
        actor,
        input=f"# {task}\n\nDo it.\n",
    )
    lattice(client, cwd, "status", task, "planned", "--no-auto-review", "--actor", actor)
