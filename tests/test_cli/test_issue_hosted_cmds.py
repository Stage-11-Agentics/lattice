"""``lattice issue ...`` on a bound checkout (LAT-368): a real loopback server,
real bound clones, the CLI run in-process.

Reads come from the synced, read-only cache; every write is a named server
operation. These tests cover the routing, the order of reads and locks, the
guidance messages, and the failure paths (SPEC §9.4, §9.5, §8.12).
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.ops import Caller, execute
from lattice.ops.base import OpContext, registered_operations
from lattice.ops import issue_common
from lattice.remote import http, session
from lattice.server import admin, tokens
from tests.issue_media_helpers import png
from tests.test_remote import hosted as _hosted
from tests.test_remote.hosted import (
    TOKEN_ENV,
    HostedEnv,
    make_repo,
    run_cli,
)

ACTOR = ("--actor", "agent:tester")
#: The fixture lives beside the other hosted-checkout fixtures; re-exported for this module.
hosted_env = _hosted.hosted_env
ISSUE_OPS = sorted(name for name in registered_operations() if name.startswith("issue."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def enable_issues(env: HostedEnv, slug: str = "demo", value: bool = True) -> None:
    admin.set_project_config(env.server_root, slug, {"issues.enabled": value})


def attach(env: HostedEnv, tmp_path: Path, name: str = "work", slug: str = "demo") -> Path:
    repo = make_repo(tmp_path / name / "repo")
    result = run_cli(repo, "remote", "attach", "team", slug)
    assert result.exit_code == 0, result.output
    return repo


@pytest.fixture()
def repo(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    enable_issues(hosted_env)
    return attach(hosted_env, tmp_path)


def run(repo: Path, *args: str, input: str | None = None, write: bool = False):  # noqa: ANN201
    return run_cli(repo, *args, "--json", *(ACTOR if write else ()), input=input)


def ok(repo: Path, *args: str, input: str | None = None, write: bool = False) -> dict:
    result = run(repo, *args, input=input, write=write)
    assert result.exit_code == 0, (args, result.output)
    return json.loads(result.stdout)["data"]


def err(repo: Path, *args: str, input: str | None = None, write: bool = False) -> dict:
    result = run(repo, *args, input=input, write=write)
    assert result.exit_code == 1, (args, result.output)
    return json.loads(result.stdout)["error"]


def file_issue(repo: Path, title: str, *extra: str) -> dict:
    return ok(repo, "issue", "file", title, *extra, write=True)


def server_events(env: HostedEnv, issue_id: str) -> list[dict]:
    path = env.board / "issues" / "events" / f"{issue_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def tree(path: Path) -> dict[str, str]:
    """Every entry under *path* (directories included), for "nothing was created"."""
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames:
            found[(Path(dirpath) / name).relative_to(path).as_posix() + "/"] = "dir"
        for name in filenames:
            full = Path(dirpath) / name
            found[full.relative_to(path).as_posix()] = str(full.stat().st_size)
    return found


# ---------------------------------------------------------------------------
# Every issue operation goes through the server
# ---------------------------------------------------------------------------


def test_every_issue_operation_works_through_the_server(
    hosted_env: HostedEnv, repo: Path, tmp_path: Path
) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(png(8, 6))
    task = ok(repo, "create", "A task", write=True)["short_id"]

    one = file_issue(repo, "First", "--description", "words", "--confidence", "definite")
    two = file_issue(repo, "Second")
    three = file_issue(repo, "Third")
    four, five = file_issue(repo, "Fourth"), file_issue(repo, "Fifth")
    first, second, third = one["short_id"], two["short_id"], three["short_id"]

    ok(repo, "issue", "link", first, task, write=True)
    ok(repo, "issue", "unlink", first, task, write=True)
    ok(repo, "issue", "link", first, task, write=True)
    ok(repo, "issue", "dismiss", second, "--reason", "noise", write=True)
    ok(repo, "issue", "reopen", second, write=True)
    duplicate = ok(repo, "issue", "duplicate", third, "--of", first, write=True)
    assert duplicate["closure"]["duplicate_of"] == one["id"]
    ok(repo, "issue", "edit", first, "--title", "First, corrected", write=True)
    ok(repo, "issue", "comment", first, "a remark", write=True)
    attached = ok(repo, "issue", "attach", first, str(shot), write=True)
    assert [m["n"] for m in attached["media"]] == [1]
    assert ok(repo, "issue", "media", first)["media"][0]["available"] == "remote"
    ok(repo, "issue", "detach", first, "1", "--reason", "pii", write=True)
    promoted = ok(repo, "issue", "promote", four["short_id"], five["short_id"], write=True)
    assert [v["state"] for v in promoted["issues"]] == ["linked", "linked"]

    seen = {
        event["type"]
        for issue in (one, two, three, four)
        for event in server_events(hosted_env, issue["id"])
    }
    assert seen >= {
        "issue_filed",
        "issue_linked",
        "issue_unlinked",
        "issue_dismissed",
        "issue_reopened",
        "issue_marked_duplicate",
        "issue_edited",
        "issue_comment_added",
        "issue_media_added",
        "issue_media_removed",
    }
    # The client wrote nothing of its own: its issue files are the server's, byte for byte.
    server_issues = hosted_env.board / "issues"
    cache_issues = repo / ".lattice" / "issues"
    for path in sorted(server_issues.rglob("*")):
        relative = path.relative_to(server_issues)
        if path.is_file() and relative.parts[:1] != ("media",):
            assert (cache_issues / relative).read_bytes() == path.read_bytes(), relative
    assert not (cache_issues / "media").exists()


# ---------------------------------------------------------------------------
# A cache is never written directly
# ---------------------------------------------------------------------------


def synced_cache(repo: Path) -> Path:
    assert run_cli(repo, "issue", "list", "--json").exit_code == 0
    cache = repo / ".lattice"
    assert (cache / "cache" / "state.json").exists()
    return cache


@pytest.mark.parametrize("op", ISSUE_OPS)
def test_a_direct_issue_write_to_a_cache_fails_before_it_creates_anything(
    repo: Path, op: str
) -> None:
    cache = synced_cache(repo)
    before = tree(cache)

    with pytest.raises(OpError) as refused:
        execute(
            cache, op, {"title": "x", "issue": "I1"}, Caller(actor="agent:tester"), run_hooks=False
        )

    assert refused.value.code in {"BOARD_IS_CACHE", "LOCAL_ONLY"}
    assert tree(cache) == before  # no issues path, no lock file, no directory


@pytest.mark.parametrize("enabled", [True, False])
def test_the_issue_gate_refuses_a_cache_before_it_looks_at_the_log(
    repo: Path, enabled: bool
) -> None:
    cache = synced_cache(repo)
    before = tree(cache)
    ctx = OpContext(
        lattice_dir=cache,
        config={"issues": {"enabled": enabled}},
        actor="agent:tester",
        caller=Caller(actor="agent:tester"),
        op_name="issue.file",
        run_hooks=False,
    )

    with pytest.raises(OpError) as refused:
        issue_common.require_issue_log(ctx)

    assert refused.value.code == "LOCAL_ONLY" and "read-only" in refused.value.message
    assert refused.value.details == {"board": "cache"}
    assert tree(cache) == before


# ---------------------------------------------------------------------------
# stdin is read before the hosted read lock
# ---------------------------------------------------------------------------


class Order:
    """Records the order of stdin reads, syncs, and read-lock takes, and proves
    at each stdin read that a writer could take the cache lock at once."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
        from lattice.cli import issue_cmds

        self.calls: list[str] = []
        self.repo = repo
        self.main = threading.get_ident()
        real_read = issue_cmds._read_stdin_text
        real_fresh, real_hold = session.ensure_fresh, session.hold_read_lock

        def read() -> str:
            self.calls.append("stdin")
            self.assert_lock_free()
            return real_read()

        def fresh(hosted, **kwargs):  # noqa: ANN001, ANN003, ANN202
            self.calls.append("sync")
            return real_fresh(hosted, **kwargs)

        def hold(hosted):  # noqa: ANN001, ANN202
            self.calls.append("read_lock")
            return real_hold(hosted)

        monkeypatch.setattr(issue_cmds, "_read_stdin_text", read)
        monkeypatch.setattr(session, "ensure_fresh", fresh)
        monkeypatch.setattr(session, "hold_read_lock", hold)

    def assert_lock_free(self) -> None:
        assert not session._locks, "the read lock is held while stdin is being read"
        lock = self.repo / ".lattice" / "locks" / "cache_rw.lock"
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if a reader holds it
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def assert_stdin_first(self) -> None:
        assert self.calls.count("stdin") == 1, self.calls
        assert self.calls[0] == "stdin", self.calls
        assert "read_lock" in self.calls, self.calls


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("issue", "file", "-"), id="file-title"),
        pytest.param(("issue", "file", "A title", "--description", "-"), id="file-description"),
        pytest.param(("issue", "edit", "{issue}", "--description", "-"), id="edit-description"),
        pytest.param(("issue", "comment", "{issue}", "-"), id="comment"),
    ],
)
def test_stdin_is_read_before_the_cache_is_synced_or_locked(
    repo: Path, monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...]
) -> None:
    existing = file_issue(repo, "Existing")
    argv = tuple(existing["short_id"] if part == "{issue}" else part for part in argv)
    order = Order(monkeypatch, repo)

    result = run(repo, *argv, input="piped text\n", write=True)

    assert result.exit_code == 0, result.output
    order.assert_stdin_first()
    view = json.loads(result.stdout)["data"]
    shown = ok(repo, "issue", "show", view["short_id"])
    assert "piped text" in json.dumps(shown)


# ---------------------------------------------------------------------------
# Disabled-guidance messages
# ---------------------------------------------------------------------------

KEPT = "Existing issues are kept"


def second_project(env: HostedEnv, monkeypatch: pytest.MonkeyPatch, slug: str) -> None:
    """A project named *slug* on the running server, reachable with the test's token variable."""
    admin.create_project(env.server_root, slug, code="BET")
    minted = tokens.create_token(
        env.server_root, user="human:alice", machine="laptop", all_projects=True
    )
    monkeypatch.setenv(TOKEN_ENV, minted["token"])


@pytest.mark.parametrize(
    "command", [("issue", "list"), ("issue", "file", "t"), ("issue", "show", "I1")]
)
def test_a_fresh_hosted_board_names_the_server_command_without_the_kept_clause(
    hosted_env: HostedEnv,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: tuple[str, ...],
) -> None:
    second_project(hosted_env, monkeypatch, "beta-proj")
    repo = attach(hosted_env, tmp_path, slug="beta-proj")

    error = err(repo, *command, write=command[1] == "file")

    assert error["code"] == "ISSUES_DISABLED"
    assert "lattice server project config beta-proj --set issues.enabled=true" in error["message"]
    assert "demo" not in error["message"]  # the slug comes from this checkout's binding
    assert KEPT not in error["message"]


def test_an_empty_cache_scaffold_does_not_claim_kept_issues(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo = attach(hosted_env, tmp_path)
    error = err(repo, "issue", "list")

    # The sync gave the cache its empty issues/ scaffold; there is nothing to keep.
    assert (repo / ".lattice" / "issues").is_dir()
    assert error["code"] == "ISSUES_DISABLED" and KEPT not in error["message"]


def test_real_issue_metadata_adds_the_kept_clause_once_the_log_is_off_again(
    hosted_env: HostedEnv, repo: Path
) -> None:
    file_issue(repo, "Observed")
    enable_issues(hosted_env, value=False)

    error = err(repo, "issue", "list")

    assert error["code"] == "ISSUES_DISABLED"
    assert "lattice server project config demo --set issues.enabled=true" in error["message"]
    assert error["message"].endswith(f"{KEPT} and reappear when it is on.")


def test_the_server_says_the_same_when_it_refuses_a_disabled_write_itself(
    hosted_env: HostedEnv,
) -> None:
    status, _, body = hosted_env.handle.op(
        "demo", "issue.file", {"title": "x"}, token=hosted_env.token, actor="agent:tester"
    )

    assert status != 200
    assert body["error"]["code"] == "ISSUES_DISABLED"
    assert (
        "lattice server project config demo --set issues.enabled=true" in body["error"]["message"]
    )


def test_an_unreadable_snapshot_names_the_offline_rebuild_on_the_server_host(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = file_issue(repo, "Will be damaged")
    snapshot = repo / ".lattice" / "issues" / f"{issue['id']}.json"
    real_fresh = session.ensure_fresh

    def damaged_after_sync(hosted, **kwargs):  # noqa: ANN001, ANN003, ANN202
        # The next sync would put a damaged cache file right; damage it once
        # the command's own sync is over, as a disk fault would.
        real_fresh(hosted, **kwargs)
        snapshot.chmod(0o600)
        snapshot.write_text("{ not json")

    monkeypatch.setattr(session, "ensure_fresh", damaged_after_sync)

    result = run_cli(repo, "issue", "list")

    assert result.exit_code == 0, result.output
    assert "is unreadable" in result.stderr
    assert "lattice rebuild --all --offline-maintenance" in result.stderr
    assert "server host" in result.stderr


# ---------------------------------------------------------------------------
# A server that does not know the issue operations
# ---------------------------------------------------------------------------


class OldServer:
    """Answers every ``issue.*`` operation as an older server would; records the posts."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
        self.posts: list[str] = []
        real = http.request

        def request(remote, method, path, **kwargs):  # noqa: ANN001, ANN003, ANN202
            if method == "POST" and "/ops/" in path:
                op = path.rsplit("/ops/", 1)[1]
                self.posts.append(op)
                if op.startswith("issue."):
                    raise http.ServerError(
                        code, f"Unknown operation '{op}'.", {"op": op}, status=404
                    )
            return real(remote, method, path, **kwargs)

        monkeypatch.setattr(http, "request", request)


@pytest.mark.parametrize("code", ["UNKNOWN_OP", "LOCAL_ONLY"])
@pytest.mark.parametrize(
    "argv",
    [
        ("issue", "file", "Anything"),
        ("issue", "comment", "I1", "text"),
        ("issue", "dismiss", "I1", "--reason", "r"),
        ("issue", "promote", "I1"),
    ],
    ids=lambda a: a[1],
)
def test_an_old_server_gets_upgrade_guidance_after_the_request_reached_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch, code: str, argv: tuple[str, ...]
) -> None:
    assert run_cli(repo, "issue", "list").exit_code == 0  # warms every cached fact
    old = OldServer(monkeypatch, code)

    error = err(repo, *argv, write=True)

    assert old.posts == [f"issue.{argv[1]}"], "the request must actually reach the server"
    assert error["code"] == code
    assert "upgrade the server" in error["message"]


def test_ordinary_unknown_operations_are_not_blamed_on_the_issue_log(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = http.request

    def request(remote, method, path, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if method == "POST" and path.endswith("/ops/task.create"):
            raise http.ServerError(
                "UNKNOWN_OP", "Unknown operation 'task.create'.", {}, status=404
            )
        return real(remote, method, path, **kwargs)

    monkeypatch.setattr(http, "request", request)
    error = err(repo, "create", "x", write=True)

    assert error["code"] == "UNKNOWN_OP" and "upgrade the server" not in error["message"]


def test_stale_cached_server_info_never_decides_the_issue_operations_fail(
    hosted_env: HostedEnv, repo: Path
) -> None:
    assert run_cli(repo, "issue", "list").exit_code == 0
    info_path = repo / ".lattice" / "cache" / "server_info.json"
    info = json.loads(info_path.read_text())
    info["event_types"] = [t for t in info["event_types"] if not t.startswith("issue_")]
    info_path.chmod(0o600)
    info_path.write_text(json.dumps(info))

    view = file_issue(repo, "Still works")

    assert view["short_id"] == "DEM-I1"
    assert server_events(hosted_env, view["id"])[0]["type"] == "issue_filed"


# ---------------------------------------------------------------------------
# issue.promote is one transaction on the server
# ---------------------------------------------------------------------------


def test_a_promote_that_fails_on_the_server_rolls_back_and_names_no_task(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one, two = file_issue(repo, "One"), file_issue(repo, "Two")
    real_link = issue_common.link_one
    calls: list[str] = []

    def second_link_fails(ctx, issue_id, task_id, params):  # noqa: ANN001, ANN202
        calls.append(issue_id)
        if len(calls) == 2:
            raise OpError("CONFLICT", "the second link failed")
        return real_link(ctx, issue_id, task_id, params)

    monkeypatch.setattr(issue_common, "link_one", second_link_fails)

    error = err(repo, "issue", "promote", one["short_id"], two["short_id"], write=True)

    assert (
        len(calls) == 2
    )  # the task existed and the first link was written, inside the transaction
    assert error["code"] == "CONFLICT"
    assert "Nothing was committed" in error["message"]
    assert "Created task" not in error["message"] and "issue link" not in error["message"]
    # Rolled back: no task, and neither issue was linked.
    assert ok(repo, "list") == []
    for issue in (one, two):
        assert ok(repo, "issue", "show", issue["short_id"])["tasks"] == []
        types = [e["type"] for e in server_events(hosted_env, issue["id"])]
        assert types == ["issue_filed"]


# ---------------------------------------------------------------------------
# issue duplicate reads its source after the post-write catch-up
# ---------------------------------------------------------------------------


def test_issue_duplicate_reads_the_source_after_catch_up_under_the_read_lock(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.storage import issues as issue_storage

    dup = file_issue(repo, "The duplicate")
    assert run_cli(repo, "issue", "list").exit_code == 0
    state = {"filed": False, "caught_up": False}
    reads: list[tuple[str, bool, bool]] = []
    main = threading.get_ident()
    real_fresh, real_catch_up = session.ensure_fresh, session.catch_up_and_report
    real_read = issue_storage.read_issue_snapshot

    def fresh(hosted, **kwargs):  # noqa: ANN001, ANN003, ANN202
        real_fresh(hosted, **kwargs)
        if not state["filed"]:
            state["filed"] = True  # the original appears after this command's read phase
            hosted_env.server_op("issue.file", {"title": "The original"})

    def catch_up(hosted, **kwargs):  # noqa: ANN001, ANN003, ANN202
        result = real_catch_up(hosted, **kwargs)
        state["caught_up"] = True
        return result

    def read(lattice_dir, issue_id, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if threading.get_ident() == main:
            reads.append((issue_id, state["caught_up"], bool(session._locks)))
        return real_read(lattice_dir, issue_id, *args, **kwargs)

    monkeypatch.setattr(session, "ensure_fresh", fresh)
    monkeypatch.setattr(session, "catch_up_and_report", catch_up)
    monkeypatch.setattr(issue_storage, "read_issue_snapshot", read)

    result = run_cli(repo, "issue", "duplicate", dup["short_id"], "--of", "DEM-I2", *ACTOR)

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"Marked {dup['short_id']} as a duplicate of DEM-I2"
    original = [r for r in reads if r[0] != dup["id"]]
    assert original, "the source snapshot must be read to name it"
    assert all(caught_up and locked for _id, caught_up, locked in original), original
