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
  the maintenance commands of SPEC §3.5 refuse with ``LOCAL_ONLY``,
  ``PLAN_REQUIRED`` appends the ``plan write`` hint (SPEC §3.9), and a refused
  claim appends its no-assignment/no-status clause after that hint. Each
  normalization checks the hosted form exactly before rewriting it.

Every durable mutation the server makes is recorded (``MutationLog``) for the
no-delete assertion of G-2.
"""

from __future__ import annotations

import itertools
import json
import os
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
    if parts[:2] == ["issues", "media"]:
        return False
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
                if path.relative_to(lattice_dir).parts[:2] == ("issues", "media"):
                    continue
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
    #: The server transaction that made it (one per write, SPEC §8.6).
    txn: int = 0


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

    A permitted relocation removes a path only after the same transaction wrote
    its relocated copy (copy first); a copy an earlier transaction wrote does not count. Test fixtures and non-durable paths are exempt.
    """
    bad: list[Mutation] = []
    for i, m in enumerate(mutations):
        if m.kind != "unlink" or m.op == FIXTURE_OP or not is_durable(m.path):
            continue
        copies = _relocated_copies(m.op, m.path)
        written_before = [
            w.path for w in mutations[:i] if w.txn == m.txn and w.kind in ("create", "replace")
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
    serials = itertools.count(1)

    def tracked(self: MutationTracker, path: Path, kind: str) -> None:
        original(self, path, kind)
        rel = path.resolve().relative_to(self.board).as_posix()
        # One tracker per server write: its serial names the transaction.
        txn = self.__dict__.setdefault("_parity_txn", next(serials))
        project = CURRENT_PROJECT.get() or "?"
        with log._lock:
            log.entries.append(Mutation(project, self.op, rel, kind, txn))

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
    """One in-process server for a worker's scenarios, recording every mutation.

    Audit is off: parity is about outputs and boards, and this server outlives the
    parity tests, so its committer's debounced ``git`` calls would otherwise land
    inside whichever test the worker runs next (one that mocks ``subprocess``, say)."""
    root = make_root(base, config={"audit": {"enabled": False}})
    person, strict = _mint(root)
    from lattice.core.tasks import set_unknown_type_reporter

    with recording_mutations() as mutations, running_server(root) as handle:
        # The server installs a process-wide reporter for unknown event types.
        # This server outlives the parity tests (one per worker), so clear the
        # process-wide reporter before other tests use local replay.
        set_unknown_type_reporter(None)
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

    @contextmanager
    def dashboard(self, root: Path) -> Iterator[Any]:
        """The bound checkout's dashboard, as ``lattice dashboard`` serves it: writes
        through the server as the browser actor (SPEC §8.3, §9.6)."""
        from lattice.boards import resolve_board
        from lattice.dashboard.bound import bound_dashboard

        with bound_dashboard(resolve_board(root)) as board:
            yield board

    @property
    def binding(self) -> str:
        """The checkout's ``<alias>/<project>``, as hosted messages name it."""
        return f"{REMOTE}/{self.slug}"

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

# Hosted-only task errors name the server-host configuration command; SPEC §3.9
# also requires the plan hint and PLAN_REQUIRED to name the plan-write command.
_PLAN_HINT = re.compile(
    r"Next: write the plan with 'lattice plan write (?P<label>[^' ]+) --file <path>', "
    r"then move to planned\."
)
_PLAN_REQUIRED_SUFFIX = re.compile(
    r"(Override with --force --reason\.) Write the plan with "
    r"`lattice plan write [^` ]+ --file <path>`\."
    r"( No assignment or status change was made\.)?"
)
_HOSTED_TASK_TYPE_HINT = re.compile(
    r"On a hosted board, ask an admin on the server host to run `lattice server project "
    r"config (?P<slug>[^/ `]+) --set '(?P<assignment>task_types=\[[^`]*\])'`; "
    r"this replaces the list, so include the existing values when adding a type\."
)
_INVALID_TASK_TYPE_PREFIX = re.compile(
    r"(?:Error: )?Invalid task type: '(?P<rejected>[^']+)'\. "
    r"Valid types: (?P<valid>.+)\."
)


def local_only_step(args: list[str], command: str, binding: str) -> dict[str, Any]:
    """The complete step a bound checkout records for *command* (SPEC §3.5): exit 1,
    the ``LOCAL_ONLY`` refusal and nothing else, plain (stderr) or ``--json`` (stdout)."""
    from lattice.boards import local_only_error

    message = local_only_error(command, binding).message
    if "--json" in args:
        body = {"ok": False, "error": {"code": "LOCAL_ONLY", "message": message}}
        return {"args": args, "exit_code": 1, "stdout": {"json": body}, "stderr": {"lines": []}}
    return {
        "args": args,
        "exit_code": 1,
        "stdout": {"lines": []},
        "stderr": {"lines": [f"Error: {message}"]},
    }


CACHE_DOCTOR_LINE = "\u2713 Cache matches the server"


class UndeclaredDifference(AssertionError):
    """A hosted step that differs from its local form in a way SPEC does not declare."""


def declared_differences(capture: dict[str, Any], *, binding: str) -> dict[str, Any]:
    """A hosted capture with SPEC's declared hosted differences put back in their
    local form. Every rewrite first checks the hosted form exactly (a whole
    ``LOCAL_ONLY`` step, exactly one doctor cache line, the exact hint text), so any
    other change still fails the comparison or raises :class:`UndeclaredDifference`.
    *binding* is the checkout's ``<alias>/<project>``."""
    ids = (capture["board"].get("ids.json") or {}).get("json", {}).get("map", {})

    def local_hint(match: re.Match[str]) -> str:
        task_id = ids.get(match.group("label"), match.group("label"))
        return f"Next: write the plan in plans/{task_id}.md, then move to planned."

    def local_task_type_hint(value: str) -> str:
        marker = " On a hosted board, "
        if marker not in value:
            return value
        prefix, _, suffix = value.partition(marker)
        hint = "On a hosted board, " + suffix
        match = _HOSTED_TASK_TYPE_HINT.fullmatch(hint)
        if match is None:
            raise UndeclaredDifference(f"unexpected hosted task-type hint: {value}")
        expected_slug = binding.rsplit("/", 1)[-1]
        if match.group("slug") != expected_slug:
            raise UndeclaredDifference(f"task-type hint names the wrong project: {value}")
        prefix_match = _INVALID_TASK_TYPE_PREFIX.fullmatch(prefix)
        if prefix_match is None:
            raise UndeclaredDifference(f"unexpected task-type error prefix: {value}")
        try:
            assignment, raw_types = match.group("assignment").split("=", 1)
            suggested = json.loads(raw_types)
        except ValueError as exc:
            raise UndeclaredDifference(f"invalid task-type assignment: {value}") from exc
        rejected = prefix_match.group("rejected")
        if (
            assignment != "task_types"
            or not isinstance(suggested, list)
            or not all(isinstance(task_type, str) for task_type in suggested)
            or not suggested
        ):
            raise UndeclaredDifference(f"unexpected task-type assignment: {value}")
        if suggested[-1] != rejected or rejected in suggested[:-1]:
            raise UndeclaredDifference(
                f"task-type assignment does not append the rejected type: {value}"
            )
        if prefix_match.group("valid") != ", ".join(suggested[:-1]):
            raise UndeclaredDifference(f"task-type assignment does not preserve the list: {value}")
        local_guidance = "On a local board, add the type to `.lattice/config.json` `task_types`."
        return f"{prefix} {local_guidance}"

    def fix(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if isinstance(value, str):
            if " On a hosted board, " in value:
                value = local_task_type_hint(value)
            value = _PLAN_HINT.sub(local_hint, value)
            return _PLAN_REQUIRED_SUFFIX.sub(r"\1\2", value)
        return value

    steps = []
    for step in capture["steps"]:
        args = step.get("args") or []
        command = local_only_command(args)
        if command is not None:
            # SPEC §3.5: the whole step must be the refusal, nothing more.
            if step != local_only_step(args, command, binding):
                raise UndeclaredDifference(f"not the LOCAL_ONLY refusal: {step}")
            steps.append(local_only_marker(step))
            continue
        step = fix(step)
        lines = (step.get("stdout") or {}).get("lines")
        if args[:1] == ["doctor"] and lines is not None:
            # SPEC §9.6: doctor on a cache adds exactly one line, its manifest check.
            if lines.count(CACHE_DOCTOR_LINE) != 1:
                raise UndeclaredDifference(f"doctor on a cache: expected one cache line: {step}")
            lines = [line for line in lines if line != CACHE_DOCTOR_LINE]
            step = {**step, "stdout": {"lines": lines}}
        steps.append(step)
    return {**capture, "steps": steps}


def hosted_target(server: ParityServer, scenario: Scenario) -> HostedTarget:
    """A fresh project and checkout target for *scenario*. A board with hooks is
    replayed on a machine whose remote opts into running them (SPEC §3.4, G-10)."""
    settings = {"run_board_hooks": True} if scenario.config.get("hooks") else {}
    return HostedTarget(server, server.new_slug(scenario.name), settings)


# ---------------------------------------------------------------------------
# The replay check
# ---------------------------------------------------------------------------

#: Scenarios the hosted replay leaves out, each with the ticket that owns the gap
#: (none since H-13a: the settings POST goes through the bound dashboard).
NOT_HOSTED: dict[str, str] = {}

#: The hosted replays, in three groups of about equal time, one test file each
#: (``test_hosted_parity.py``, ``_2``, ``_3``), so no file exceeds the per-file budget.
HOSTED_GROUPS: tuple[tuple[str, ...], ...] = (
    (
        "lifecycle",
        "hooks_sentinel",
        "tombstones",
        "plan_read",
        "project_codes",
        "rejections",
        "complete_via",
    ),
    (
        "artifacts",
        "review_cycles",
        "comments",
        "prose_writes",
        "reviews",
        "completion_git_policy",
        "maintenance",
    ),
    (
        "claims",
        "links",
        "sessions",
        "resources",
        "criteria",
        "plan_integrity",
        "flags",
        "dashboard_settings",
        "issues",
    ),
)


def hosted_cases(group: int) -> list[Any]:
    """``pytest.param(scenario, mode)`` for every replay of *group*."""
    import pytest

    from tests.parity.corpus import SCENARIOS
    from tests.parity.record import MODES

    names = HOSTED_GROUPS[group]
    return [
        pytest.param(s, m, id=f"{s.name}.{m}") for s in SCENARIOS if s.name in names for m in MODES
    ]


def read_board(root: Path, args: list[str], extra_env: dict[str, str]) -> tuple[int, str, str]:
    """``lattice <args>`` read in-process against the board at *root*: exit code,
    stdout, and the type of any exception (a corrupt board crashes some reads,
    locally as well)."""
    from lattice.cli.main import cli
    from tests.parity.record import _base_env, _chdir, _process_env, _runner

    env = {**_base_env(root), **extra_env}
    with _chdir(root), _process_env(env):
        result = _runner().invoke(cli, args, env=env)
    exc = result.exception
    crash = "" if exc is None or isinstance(exc, SystemExit) else type(exc).__name__
    return result.exit_code, result.stdout.replace(str(root), "<ROOT>"), crash


def board_reads(lattice_dir: Path) -> list[list[str]]:
    """Read commands covering every task, active and archived."""
    commands = [["list", "--json"], ["list", "--include-archived", "--json"]]
    ids = json.loads((lattice_dir / "ids.json").read_text())["map"]
    for short in sorted(ids):
        commands.append(["show", short, "--json", "--compact"])
    return commands


def assert_reads_match(
    checkout: Path, server_project: Path, cache: Path, env: dict[str, str]
) -> None:
    """Read commands print the same through the cache as on the server's own board.

    The cache has just caught up, so the reads run as they do beside a live
    follower (SPEC §9.5): straight from the cache, with no catch-up request each.
    """
    from datetime import UTC, datetime, timedelta

    from lattice.remote.follower import follower_path

    marker = follower_path(checkout)
    until = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    marker.write_text(json.dumps({"pid": os.getpid(), "stream_live_until": until}))
    try:
        for args in board_reads(cache):
            on_cache = read_board(checkout, args, env)
            on_server = read_board(server_project, args, {})
            assert on_cache == on_server, f"lattice {' '.join(args)} differs on cache and server"
    finally:
        marker.unlink()


def check_scenario_through_the_server(
    scenario: Scenario, mode: str, server: ParityServer, tmp_path: Path
) -> None:
    """Replay *scenario* through a bound checkout and check it (AC-5, AC-9, G-2):
    the golden's output and board, the cache equal to the server board, reads
    agreeing on both, and no unpermitted removal."""
    from tests.parity.record import load_golden, run_scenario

    target = hosted_target(server, scenario)
    checkout = tmp_path / "board"
    capture = run_scenario(scenario, checkout, mode=mode, target=target)

    expected = comparable(load_golden(scenario.name, mode))
    actual = comparable(declared_differences(capture, binding=target.binding))
    assert actual["steps"] == expected["steps"], f"hosted output drift in {scenario.name}.{mode}"
    assert actual["board"] == expected["board"], f"hosted board drift in {scenario.name}.{mode}"
    assert actual.get("sentinel") == expected.get("sentinel"), "hook sentinel drift"

    # AC-9: the cache is the server board, byte for byte, and reads agree.
    cache = checkout / ".lattice"
    board = server.board(target.slug)
    assert durable_tree(cache) == durable_tree(board)
    if mode == "plain":
        # Once per scenario: the plain replay runs every step (``plain_only`` ones
        # too), so the JSON replay's board adds no read coverage.
        assert_reads_match(checkout, board.parent, cache, target.env(checkout))

    # G-2: no removal of board data except a permitted relocation.
    assert server.mutations is not None
    mutations = server.mutations.of(target.slug)
    assert mutations, "the recorder saw the scenario's writes"
    assert forbidden_removals(mutations, board) == []
