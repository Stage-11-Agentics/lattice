"""Replay the golden corpus through a bound checkout against an in-process server.

AC-5, AC-9, G-2 (hosted), H-12. One server serves every scenario a pytest
worker runs (``ParityServer``); each scenario gets its own project, created
on the running server, and its own checkout holding only
``.lattice-remote.json``. The checkout's first command runs the first sync.

What differs from the local run, and why:

- **Step 0.** ``lattice init`` on a bound checkout is ``LOCAL_ONLY`` (SPEC
  §3.5). The project is created on the server with ``--code PAR`` instead,
  and the scenario's config patch is applied to the server's ``config.json``
  before the server first loads the project. Step 0 is compared as the
  golden's (``SETUP``), and the board it leaves is compared with everything else.
- **Fixture writes.** A ``WriteFile`` / ``DeleteFile`` under ``.lattice/`` is
  test setup, not a command under test. A durable path (SPEC §6.1) is written
  on the server as one journaled server transaction (``xtest.parity_fixture``),
  so the next sync carries it to the cache as any change; a runtime path
  (``review_state/``) is machine-local, so it is written in the checkout's
  cache. Anything outside ``.lattice/`` is written in the checkout.
- **Declared hosted differences** (normalized by :func:`declared_differences`):
  the maintenance commands of SPEC §3.5 refuse with ``LOCAL_ONLY``, and
  ``PLAN_REQUIRED`` appends the ``plan write`` hint (SPEC §3.9). Each
  normalization checks the hosted form exactly before rewriting it.

Every durable mutation the server makes is recorded (``MutationLog``) for the
no-delete assertion of G-2.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lattice.server import admin, tokens
from lattice.server.testing import make_root, running_server
from tests.parity.corpus import Scenario
from tests.parity.fixture_op import FIXTURE_OP
from tests.parity.record import (
    DURABLE_DIRS,
    DURABLE_FILES,
    LocalTarget,
    _patch_config,
)

REMOTE = "parity"
RUNTIME_DIRS = ("locks", "review_state", "tmp-prompts", ".daemon")


def is_durable(rel: str) -> bool:
    """*rel* (relative to ``.lattice/``) is durable board data or workspace (SPEC §6.1)."""
    parts = rel.split("/")
    if len(parts) == 1:
        return rel in DURABLE_FILES
    return parts[0] in DURABLE_DIRS


def durable_tree(lattice_dir: Path) -> dict[str, bytes]:
    """Every durable file under *lattice_dir*, path to bytes."""
    found: dict[str, bytes] = {}
    for name in DURABLE_FILES:
        path = lattice_dir / name
        if path.is_file():
            found[name] = path.read_bytes()
    for name in DURABLE_DIRS:
        base = lattice_dir / name
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file() and ".tmp." not in path.name:
                    found[path.relative_to(lattice_dir).as_posix()] = path.read_bytes()
    return dict(sorted(found.items()))


# ---------------------------------------------------------------------------
# The write recorder, server side (G-2)
# ---------------------------------------------------------------------------


@dataclass
class Mutation:
    project: str
    op: str
    path: str
    kind: str


@dataclass
class MutationLog:
    """Every durable mutation any server transaction makes, per project."""

    entries: list[Mutation] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def of(self, project: str) -> list[Mutation]:
        with self._lock:
            return [m for m in self.entries if m.project == project]


def _relocated_copies(op: str, path: str) -> tuple[str, ...] | None:
    """Where a removal SPEC §7 permits leaves its copy (any of these), or ``None``
    when *op* removing *path* is not a permitted relocation: archive and unarchive
    move a task's files between the active tree and ``archive/``, and ending a
    session moves ``sessions/<name>.json`` to ``sessions/archive/<name>_<id>.json``."""
    if op == "task.archive" and not path.startswith("archive/"):
        return (f"archive/{path}",)
    if op == "task.unarchive" and path.startswith("archive/"):
        return (path[len("archive/") :],)
    if op == "session.end" and path.startswith("sessions/") and path.count("/") == 1:
        return (f"sessions/archive/{path[len('sessions/') : -len('.json')]}_",)
    return None


def forbidden_removals(mutations: list[Mutation], board: Path) -> list[Mutation]:
    """Unlinks of durable paths that are not a relocation SPEC §7 permits.

    A permitted relocation removes a path only after the same operation wrote its
    relocated copy (copy first). Test fixtures and non-durable paths are exempt.
    """
    bad: list[Mutation] = []
    for i, m in enumerate(mutations):
        if m.kind != "unlink" or m.op == FIXTURE_OP or not is_durable(m.path):
            continue
        copies = _relocated_copies(m.op, m.path)
        written_before = [
            w.path for w in mutations[:i] if w.op == m.op and w.kind in ("create", "replace")
        ]
        if copies is None or not any(p.startswith(c) for p in written_before for c in copies):
            bad.append(m)
    return bad


@contextmanager
def recording_mutations() -> Iterator[MutationLog]:
    """Record every call of the server's per-write tracker (all projects, all threads)."""
    from lattice.server.project import CURRENT_PROJECT, MutationTracker

    log = MutationLog()
    original = MutationTracker.__call__

    def tracked(self: MutationTracker, path: Path, kind: str) -> None:
        original(self, path, kind)
        rel = path.resolve().relative_to(self.board).as_posix()
        with log._lock:
            log.entries.append(Mutation(CURRENT_PROJECT.get() or "?", self.op, rel, kind))

    MutationTracker.__call__ = tracked  # type: ignore[method-assign]
    try:
        yield log
    finally:
        MutationTracker.__call__ = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


@dataclass
class ParityServer:
    """A server the replay talks to over HTTP: in this process (with the write
    recorder) or a ``lattice server serve`` subprocess (G-5)."""

    root: Path
    url: str
    token: str
    strict_token: str
    mutations: MutationLog | None = None
    _count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def new_slug(self, stem: str) -> str:
        with self._lock:
            self._count += 1
            n = self._count
        return f"{stem.replace('_', '-')[:40]}-{n}"

    def board(self, slug: str) -> Path:
        return self.root / "projects" / slug / ".lattice"

    def create_project(self, slug: str, config_patch: dict, checkout: Path) -> None:
        """Create *slug* as ``lattice init --project-code PAR`` would, with the
        scenario's config patch (the running server loads it on first use)."""
        admin.create_project(self.root, slug, code="PAR")
        # ``lattice init --actor human:parity`` records the init actor as the default.
        patch = {"default_actor": "human:parity", **config_patch}
        _patch_config(self.board(slug), patch, checkout)

    def fixture(self, slug: str, rel: str, text: str | None) -> None:
        """Write (or, with ``text=None``, delete) one durable path as the
        ``xtest.parity_fixture`` operation: a journaled server transaction, so sync
        carries it like any change."""
        from lattice.server.testing import http_request

        status, _, body = http_request(
            "POST",
            f"{self.url}/v1/projects/{slug}/ops/{FIXTURE_OP}",
            token=self.token,
            body={"params": {"path": rel, "text": text}},
        )
        assert status == 200, body


def _mint(root: Path) -> tuple[str, str]:
    """A person token (default actor ``human:parity``) and a token with no default."""
    person = tokens.create_token(root, user="human:parity", machine="parity", all_projects=True)
    strict = tokens.create_token(
        root,
        user="human:parity",
        machine="parity",
        actors=["human:*", "agent:*"],
        all_projects=True,
    )
    return person["token"], strict["token"]


@contextmanager
def parity_server(base: Path) -> Iterator[ParityServer]:
    """One in-process server for a worker's scenarios, recording every mutation."""
    root = make_root(base)
    person, strict = _mint(root)
    with recording_mutations() as mutations, running_server(root) as handle:
        yield ParityServer(
            root=root, url=handle.url, token=person, strict_token=strict, mutations=mutations
        )


#: The commands whose operations take no actor (``no_actor``; SPEC §3.7): on a
#: server they run as the token's default actor.
ACTORLESS_COMMANDS = {
    ("session", "start"): "session.start",
    ("session", "end"): "session.end",
    ("set-project-code",): "board.set_project_code",
    ("set-subproject-code",): "board.set_subproject_code",
    ("context", "write"): "board.context_write",
    ("board", "write"): "board.file_write",
}
TOKEN_ENV = "LATTICE_PARITY_TOKEN"


def actorless(args: list[str]) -> bool:
    return any(tuple(args[: len(cmd)]) == cmd for cmd in ACTORLESS_COMMANDS)


# ---------------------------------------------------------------------------
# The hosted target
# ---------------------------------------------------------------------------

SETUP = {"setup": "project created on the server (lattice init is LOCAL_ONLY here)"}
_ID_TOKEN = re.compile(r"<ID-(\d+)>")


def local_only_command(args: list[str]) -> str | None:
    """The SPEC §3.5 command *args* runs, when it is one (``lattice.boards``' list)."""
    if not args:
        return None
    if args[0] in ("rebuild", "backfill-ids", "init"):
        return args[0]
    if args[0] == "doctor" and "--fix" in args:
        return "doctor --fix"
    if args[:2] in (["migrate", "needs-human"], ["demo", "init"]):
        return " ".join(args[:2])
    return None


def local_only_marker(step: dict[str, Any]) -> dict[str, Any]:
    """A maintenance command's step reduced to its arguments: locally it runs, on a
    bound checkout it refuses with ``LOCAL_ONLY`` (checked by
    :func:`declared_differences`). The board after the scenario is still compared."""
    command = local_only_command(step.get("args") or [])
    return {"args": step["args"], "local_only": command} if command else step


def comparable(capture: dict[str, Any]) -> dict[str, Any]:
    """*capture* without step 0 (``init`` or the server-side setup), its
    ``<ID-n>`` placeholders renumbered in first-seen order over the steps, then
    the board, then the sentinel (the normalizer's own order): two captures
    that differ only in step 0 compare equal exactly when everything else matches."""
    seen: dict[str, str] = {}

    def renumber(value: Any) -> Any:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
        return json.loads(
            _ID_TOKEN.sub(lambda m: seen.setdefault(m.group(1), f"<ID-{len(seen) + 1}>"), text)
        )

    out = {k: v for k, v in capture.items() if k not in ("steps", "board", "sentinel")}
    out["steps"] = [renumber(local_only_marker(step)) for step in capture["steps"][1:]]
    board = capture["board"]

    def order(path: str) -> str:
        # Numbering-independent: IDs the steps showed take their new number; others sort alike.
        return _ID_TOKEN.sub(lambda m: seen.get(m.group(1), "<ID-?>"), path)

    out["board"] = {renumber(path): renumber(board[path]) for path in sorted(board, key=order)}
    if "sentinel" in capture:
        out["sentinel"] = renumber(capture["sentinel"])
    return out


@dataclass
class HostedTarget(LocalTarget):
    """A bound checkout at the scenario root, its project on *server*."""

    server: ParityServer
    slug: str
    settings: dict[str, Any] = field(default_factory=dict)

    def env(self, root: Path) -> dict[str, str]:
        home = root / "home"
        path = home / ".config" / "lattice" / "remotes.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"url": self.server.url, "token": {"env": TOKEN_ENV}, "retry_seconds": 1}
        entry.update(self.settings)
        path.write_text(json.dumps({"remotes": {REMOTE: entry}}, indent=2) + "\n")
        path.chmod(0o600)
        return {TOKEN_ENV: self.server.strict_token}

    def step_env(self, args: list[str]) -> dict[str, str]:
        """Which token a command uses. A write with no actor fails locally with
        ``MISSING_ACTOR``; on a server it runs as the token's default actor when
        the token has one (SPEC §8.3, §9.5). So every command runs with a token
        that has no default (``human:*``, ``agent:*``), which refuses it the same
        way, except the commands that take no actor, which on a server need the
        token's default (SPEC §3.7): they use a person token whose default is
        ``human:parity``, the local board's ``default_actor``."""
        return {TOKEN_ENV: self.server.token} if actorless(args) else {}

    def setup(self, scenario: Scenario, root: Path, invoke: Any) -> dict[str, Any]:
        self.server.create_project(self.slug, scenario.config, root)
        (root / ".lattice-remote.json").write_text(
            json.dumps({"project": self.slug, "remote": REMOTE}) + "\n"
        )
        return dict(SETUP)

    def finish(self, root: Path, invoke: Any) -> None:
        """Catch the cache up (a scenario may end on a fixture or a refusal), so
        the captured board is the server's as of the last step."""
        result = invoke(["sync", "--json"])
        assert result["exit_code"] == 0, result

    def fixture(self, root: Path, rel: str, text: str | None, executable: bool) -> bool:
        if not rel.startswith(".lattice/"):
            return False
        inner = rel[len(".lattice/") :]
        if is_durable(inner):
            assert not executable
            self.server.fixture(self.slug, inner, text)
            return True
        assert inner.split("/")[0] in RUNTIME_DIRS, rel
        return False  # machine-local runtime state lives in the checkout's cache


# ---------------------------------------------------------------------------
# Declared hosted differences
# ---------------------------------------------------------------------------

# SPEC §3.9: a hosted cache is read-only, so the planning hint and PLAN_REQUIRED
# name the command that writes a plan.
_PLAN_HINT = re.compile(
    r"Next: write the plan with 'lattice plan write (?P<label>[^' ]+) --file <path>', "
    r"then move to planned\."
)
_PLAN_REQUIRED_SUFFIX = re.compile(
    r"(Override with --force --reason\.) Write the plan with "
    r"`lattice plan write [^` ]+ --file <path>`\."
)


def assert_local_only(step: dict[str, Any], command: str) -> None:
    """*step* is the ``LOCAL_ONLY`` refusal of *command* (SPEC §3.5), plain or JSON."""
    from lattice.boards import local_only_error

    assert step["exit_code"] == 1, step
    body = step["stdout"].get("json")
    if body is not None:
        assert body["ok"] is False and body["error"]["code"] == "LOCAL_ONLY", step
        message = body["error"]["message"]
    else:
        message = "\n".join(step["stderr"]["lines"]).removeprefix("Error: ")
    binding = re.search(r"\('([^']+)'\)", message)
    assert binding is not None, step
    assert message == local_only_error(command, binding.group(1)).message, step


CACHE_DOCTOR_LINE = "\u2713 Cache matches the server"


def declared_differences(capture: dict[str, Any]) -> dict[str, Any]:
    """A hosted capture with SPEC's declared hosted differences put back in their
    local form. Each rewrite matches the hosted text exactly, so any other change
    still fails the comparison."""
    ids = (capture["board"].get("ids.json") or {}).get("json", {}).get("map", {})

    def local_hint(match: re.Match[str]) -> str:
        task_id = ids.get(match.group("label"), match.group("label"))
        return f"Next: write the plan in plans/{task_id}.md, then move to planned."

    def fix(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if isinstance(value, str):
            value = _PLAN_HINT.sub(local_hint, value)
            return _PLAN_REQUIRED_SUFFIX.sub(r"\1", value)
        return value

    steps = []
    for step in fix(capture["steps"]):
        command = local_only_command(step.get("args") or [])
        if command is not None:
            assert_local_only(step, command)
            step = local_only_marker(step)
        lines = (step.get("stdout") or {}).get("lines")
        if step.get("args", [None])[0] == "doctor" and lines and CACHE_DOCTOR_LINE in lines:
            # SPEC §9.6: doctor on a cache also compares it with the server's manifest.
            lines = [line for line in lines if line != CACHE_DOCTOR_LINE]
            step = {**step, "stdout": {"lines": lines}}
        steps.append(step)
    return {**capture, "steps": steps}


def hosted_target(server: ParityServer, scenario: Scenario) -> HostedTarget:
    """A fresh project and checkout target for *scenario*. A board with hooks is
    replayed on a machine whose remote opts into running them (SPEC §3.4, G-10)."""
    settings = {"run_board_hooks": True} if scenario.config.get("hooks") else {}
    return HostedTarget(server, server.new_slug(scenario.name), settings)
