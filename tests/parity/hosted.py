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
from lattice.server.testing import ServerHandle, make_root, running_server
from tests.parity.corpus import Scenario
from tests.test_remote import sync_shim
from tests.parity.record import (
    DURABLE_DIRS,
    DURABLE_FILES,
    LocalTarget,
    _patch_config,
)

REMOTE = "parity"
FIXTURE_OP = "xtest.parity_fixture"
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


#: Removals SPEC §7 permits: copy-first relocation by these operations.
RELOCATING_OPS = {
    "task.archive": ("", "archive/"),
    "task.unarchive": ("archive/", ""),
    "session.end": ("sessions/", "sessions/archive/"),
}


def forbidden_removals(mutations: list[Mutation], board: Path) -> list[Mutation]:
    """Unlinks of durable paths that are not a relocation SPEC §7 permits.

    A permitted relocation removes a path only when the same operation created
    its relocated copy, and that copy exists after the scenario (or was itself
    relocated back later, which the same rule covers). Test fixtures are exempt.
    """
    bad: list[Mutation] = []
    created: dict[str, set[str]] = {}
    for m in mutations:
        if m.kind in ("create", "replace", "append"):
            created.setdefault(m.op, set()).add(m.path)
    for m in mutations:
        if m.kind != "unlink" or m.op == FIXTURE_OP or not is_durable(m.path):
            continue
        rule = RELOCATING_OPS.get(m.op)
        if rule is None:
            bad.append(m)
            continue
        src, dst = rule
        if not m.path.startswith(src):
            bad.append(m)
            continue
        if dst + m.path[len(src) :] not in created.get(m.op, set()):
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
    root: Path
    handle: ServerHandle
    token: str
    mutations: MutationLog
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
        """Write (or, with ``text=None``, delete) one durable path as a journaled
        server transaction, so sync carries it like any change."""
        from lattice.core.ids import generate_op_id
        from lattice.ops.base import OpResult
        from lattice.server.project import MutationTracker
        from lattice.storage.fs import atomic_write, ensure_dir, recording, unlink_path

        project = self.handle.project(slug)
        assert project is not None
        board = self.board(slug)
        target = board / rel

        def work(txn: Any) -> OpResult:
            with recording(txn.before_mutation) as recorder:
                if text is None:
                    unlink_path(target)
                else:
                    ensure_dir(target.parent)
                    atomic_write(target, text)
            paths = recorder.relative_paths(board)
            return OpResult(value={"paths": paths}, paths=tuple(paths))

        with project.locked():
            if project.state != "loaded":
                project._load()
            project.admit()
            project._transact(
                op=FIXTURE_OP,
                op_id=generate_op_id(),
                token_id=None,
                fp=None,
                tracker=MutationTracker(board, FIXTURE_OP),
                work=work,
            )


@contextmanager
def parity_server(base: Path) -> Iterator[ParityServer]:
    """One server for a worker's scenarios; a token that may act as any
    ``human:`` or ``agent:`` actor and so, like the local CLI, has no default."""
    root = make_root(base)
    minted = tokens.create_token(
        root,
        user="human:parity",
        machine="parity",
        actors=["human:*", "agent:*"],
        all_projects=True,
    )
    with recording_mutations() as mutations, running_server(root) as handle:
        # TODO(rebase onto v2): H-10a's real sync route replaces the shim.
        sync_shim.install(handle.app)
        yield ParityServer(root=root, handle=handle, token=minted["token"], mutations=mutations)


# ---------------------------------------------------------------------------
# The hosted target
# ---------------------------------------------------------------------------

SETUP = {"setup": "project created on the server (lattice init is LOCAL_ONLY here)"}
_ID_TOKEN = re.compile(r"<ID-(\d+)>")


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
    out["steps"] = [renumber(step) for step in capture["steps"][1:]]
    board = capture["board"]
    # Files in the normalizer's order: their path's first ID was numbered in the steps.
    out["board"] = {renumber(path): renumber(board[path]) for path in board}
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
        entry = {"url": self.server.handle.url, "token": self.server.token, "retry_seconds": 1}
        entry.update(self.settings)
        path.write_text(json.dumps({"remotes": {REMOTE: entry}}, indent=2) + "\n")
        path.chmod(0o600)
        return {}

    def setup(self, scenario: Scenario, root: Path, invoke: Any) -> dict[str, Any]:
        self.server.create_project(self.slug, scenario.config, root)
        (root / ".lattice-remote.json").write_text(
            json.dumps({"project": self.slug, "remote": REMOTE}) + "\n"
        )
        return dict(SETUP)

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

    return {**capture, "steps": fix(capture["steps"])}
