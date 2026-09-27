"""Scenario rehearsals W (AC-40), B (AC-41) and T (AC-42), through the real client
against a ``lattice server serve`` subprocess.

- **W**: a repository whose board is tracked in git, with a feature branch cut
  before the move, moves its board by the guide's steps; then 5 worktrees and 5
  scripted writers make 200 mixed writes while a follower keeps the checkout's
  cache live. Every write is visible from every worktree within 2 s, no
  ``.lattice`` path shows in any ``git status``, ``remote status`` lists the
  pre-move branch, and after its fix, merging it brings no board file.
- **B**: 3 local clients (own clones, followers) and 3 clients behind a
  header-checking proxy (no follower), one board; integrity and freshness as W.
- **T**: a tracked board moved by the guide's steps, with a second clone that
  pulls the move; 5 tokens (5 users, 3 machines), 10 tickets through the full
  lifecycle; every event's origin names the right user and machine.

The per-PR lane runs the counts EVALUATION names; they fit in well under a minute
each on two cores.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lattice.server.testing import make_root
from tests.torture.harness import (
    Client,
    ServerProcess,
    bound_checkout,
    chmod_tree_writable,
    git,
    lattice,
    lattice_json,
    spawn_lattice,
    stop_process,
)
from tests.torture.proxy import header_proxy
from tests.torture.rehearsal import (
    FRESHNESS_SECONDS,
    cut_feature_branch,
    freshness,
    move_board,
    read_jsonl,
    start_scripted,
    tracked_board_repo,
    wait_all,
)

pytestmark = [pytest.mark.torture, pytest.mark.timeout(600)]

PRE_MOVE = "feat/pre-move"


def writer_steps(w: int, tasks: int, writes: int, actor: str) -> list[dict]:
    """*writes* mixed writes by writer *w* on *tasks* tasks of its own: creates
    first, then comments, status changes (with a plan before ``planned``),
    plan rewrites, and assignments, round-robin over its tasks."""
    steps: list[dict] = [
        {"args": ["create", f"W{w} task {k}", "--actor", actor, "--json"], "save": f"t{k}"}
        for k in range(tasks)
    ]
    stage = [0] * tasks
    k = 0
    while len(steps) < writes:
        t = k % tasks
        task = f"{{t{t}}}"
        kind = (k // tasks) % 5
        if kind == 0:
            steps.append({"args": ["comment", task, f"w{w} note {k}", "--actor", actor, "--json"]})
        elif kind == 1 and stage[t] == 0:
            steps.append({"args": ["status", task, "in_planning", "--actor", actor, "--json"]})
            stage[t] = 1
        elif kind == 2:
            steps.append(
                {
                    "args": ["plan", "write", task, "--stdin", "--actor", actor, "--json"],
                    "input": f"# plan by w{w}\n\nrevision {k}\n",
                }
            )
        elif kind == 3 and stage[t] == 1:
            steps.append(
                {
                    "args": [
                        "status",
                        task,
                        "planned",
                        "--no-auto-review",
                        "--actor",
                        actor,
                        "--json",
                    ]
                }
            )
            stage[t] = 2
        else:
            steps.append(
                {"args": ["assign", task, f"agent:owner-{w}-{k % 3}", "--actor", actor, "--json"]}
            )
        k += 1
    return steps[:writes]


def run_writers(
    work: Path,
    writers: list[tuple[Client, Path, str]],
    poll_dirs: list[tuple[Client, list[Path]]],
    *,
    tasks: int,
    writes: int,
) -> tuple[list[dict], list[dict]]:
    """Run one scripted writer per ``(client, cwd, actor)`` (*writes* in total) and
    one poller per ``(client, dirs)``; return all write records and all polls."""
    stop = work / "stop-polling"
    pollers = [
        start_scripted(
            client,
            {
                "mode": "poll",
                "cwds": [str(d) for d in dirs],
                "out": str(work / f"poll-{n}.jsonl"),
                "stop": str(stop),
            },
            work / f"poll-{n}",
        )
        for n, (client, dirs) in enumerate(poll_dirs)
    ]
    start_at = time.time() + 2.0  # every writer starts at once
    per_writer = writes // len(writers)
    procs = [
        start_scripted(
            client,
            {
                "mode": "write",
                "cwd": str(cwd),
                "steps": writer_steps(w, tasks, per_writer, actor),
                "out": str(work / f"writer-{w}.jsonl"),
                "start_at": start_at,
            },
            work / f"writer-{w}",
        )
        for w, (client, cwd, actor) in enumerate(writers)
    ]
    try:
        wait_all(procs, timeout=400)
        time.sleep(FRESHNESS_SECONDS + 1.0)  # the pollers see the last writes
    finally:
        stop.write_text("")
        wait_all(pollers, timeout=60)
    records = [r for w in range(len(writers)) for r in read_jsonl(work / f"writer-{w}.jsonl")]
    polls = [p for n in range(len(poll_dirs)) for p in read_jsonl(work / f"poll-{n}.jsonl")]
    failed = [r for r in records if r["exit"] != 0]
    assert not failed, failed[:3]
    assert len(records) == per_writer * len(writers)
    poll_errors = [p for p in polls if "error" in p]
    assert not poll_errors, poll_errors[:3]
    return records, polls


def assert_fresh(records: list[dict], polls: list[dict], dirs: list[Path]) -> dict[str, float]:
    worst = freshness(records, polls)
    assert set(worst) == {str(d) for d in dirs}, worst
    late = {cwd: delay for cwd, delay in worst.items() if delay > FRESHNESS_SECONDS}
    assert not late, f"writes visible later than {FRESHNESS_SECONDS}s: {late}"
    return worst


def server_heads(server: ServerProcess) -> dict[str, str]:
    """Every task's short ID and ``last_event_id`` on the server's own board."""
    heads = {}
    for path in (server.board() / "tasks").glob("*.json"):
        snap = json.loads(path.read_text())
        heads[snap.get("short_id") or snap["id"]] = snap["last_event_id"]
    return heads


def assert_matches_server(client: Client, cwd: Path, server: ServerProcess) -> None:
    listed = lattice_json(client, cwd, "list")
    assert {r.get("short_id") or r["id"]: r["last_event_id"] for r in listed} == server_heads(
        server
    )


def assert_doctor_clean(client: Client, cwd: Path, server: ServerProcess) -> None:
    report = json.loads(lattice(client, cwd, "doctor", "--json", check=False).stdout)
    assert report["ok"], report
    assert not [f for f in report["data"]["findings"] if f["level"] == "error"], report
    proc = lattice(
        client, cwd, "server", "project", "doctor", "demo", "--root", str(server.root), "--json"
    )
    findings = json.loads(proc.stdout)["data"]["findings"]
    assert not [f for f in findings if f["level"] == "error"], findings


def start_follower(client: Client, cwd: Path, log: Path):
    """``lattice sync --follow`` in *cwd*, returned once the cache reads as live."""
    proc = spawn_lattice(client, cwd, "sync", "--follow", log=log)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = lattice_json(client, cwd, "remote", "status")
        if status["follower"]["live"]:
            return proc
        assert proc.poll() is None, log.read_text()
        time.sleep(0.2)
    raise AssertionError(f"follower never went live: {log.read_text()}")


def lattice_paths_in_status(cwd: Path) -> list[str]:
    return [line for line in git(cwd, "status", "--porcelain").splitlines() if ".lattice" in line]


# ---------------------------------------------------------------------------
# W
# ---------------------------------------------------------------------------


def test_w(tmp_path: Path) -> None:
    server = ServerProcess(make_root(tmp_path, projects={}))
    server.start()
    follower = None
    try:
        alice = server.client(tmp_path / "alice")
        _origin, repo = tracked_board_repo(alice, tmp_path)
        cut_feature_branch(repo, PRE_MOVE)

        moved = move_board(server, alice, repo, tmp_path)
        # The old board is kept aside, ignored; the binding is committed and pushed.
        assert [p.name for p in repo.glob(".lattice.pre-hosted-*")]
        assert git(repo, "ls-files", ".lattice") == ""
        assert git(repo, "status", "--porcelain") == ""
        assert git(repo, "log", "-1", "--format=%s") == "Move the Lattice board to the server"
        # remote status lists the branch cut before the move, local and remote-tracking.
        assert PRE_MOVE in moved.status["branches_tracking_board"], moved.status
        assert f"origin/{PRE_MOVE}" in moved.status["branches_tracking_board"]
        # The tracked history arrived: the plan and the comment made before the move.
        shown = lattice_json(alice, repo, "show", "DEM-1")
        assert shown["comment_count"] == 1
        assert "The tracked plan." in lattice(alice, repo, "plan", "DEM-1").stdout

        worktrees = [repo.parent / f"wt-{n}" for n in range(5)]
        for n, wt in enumerate(worktrees):
            git(repo, "worktree", "add", "-q", "-b", f"work-{n}", str(wt))
        follower = start_follower(alice, repo, tmp_path / "follower.log")

        dirs = [repo, *worktrees]
        records, polls = run_writers(
            tmp_path,
            [(alice, wt, f"agent:writer-{n}") for n, wt in enumerate(worktrees)],
            [(alice, dirs)],
            tasks=4,
            writes=200,
        )
        worst = assert_fresh(records, polls, dirs)
        print(f"W worst visibility delay per directory: {worst}")
        for cwd in dirs:
            assert_matches_server(alice, cwd, server)
            assert lattice_paths_in_status(cwd) == [], cwd
        assert stop_process(follower) == 0
        follower = None

        # Fix the pre-move branch (the guide's `git rm -r --cached`), then merge it.
        fix = repo.parent / "fix"
        git(repo, "worktree", "add", "-q", str(fix), PRE_MOVE)
        git(fix, "rm", "-r", "--cached", "-q", ".lattice")
        git(fix, "commit", "-q", "-m", "untrack the board")
        git(fix, "push", "-q", "origin", PRE_MOVE)
        git(repo, "worktree", "remove", "--force", str(fix))
        assert lattice_json(alice, repo, "remote", "status")["branches_tracking_board"] == []
        before = server_heads(server)
        git(repo, "merge", "-q", "--no-edit", PRE_MOVE)
        assert git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", ".lattice") == ""
        merged = git(repo, "diff", "--name-only", "HEAD~1", "HEAD")
        assert merged.splitlines() == ["feature.txt"], merged
        assert lattice_paths_in_status(repo) == []
        assert_matches_server(alice, worktrees[0], server)
        assert server_heads(server) == before
        assert_doctor_clean(alice, worktrees[0], server)
    finally:
        if follower is not None:
            stop_process(follower)
        server.stop()
        chmod_tree_writable(tmp_path)


# ---------------------------------------------------------------------------
# B
# ---------------------------------------------------------------------------

PROXY_HEADERS = {
    "X-Access-Client-Id": ("TORTURE_PROXY_ID", "box-id"),
    "X-Access-Client-Secret": ("TORTURE_PROXY_SECRET", "box-secret-value"),
}


def test_b(tmp_path: Path) -> None:
    server = ServerProcess(make_root(tmp_path, projects={"demo": {"code": "DEM"}}))
    server.start()
    follower = None
    try:
        with header_proxy(
            server.url, {name: value for name, (_, value) in PROXY_HEADERS.items()}
        ) as proxy:
            laptop = server.client(tmp_path / "laptop", user="human:alice", machine="laptop")
            box = server.client(
                tmp_path / "box",
                user="human:alice",
                machine="box",
                url=proxy.url,
                headers=PROXY_HEADERS,
            )
            origin = tmp_path / "origin.git"
            git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
            repo = bound_checkout(laptop, tmp_path / "laptop-repo")
            git(repo, "remote", "add", "origin", str(origin))
            git(repo, "push", "-q", "-u", "origin", "main")

            # The laptop: three worktrees of one checkout, one follower.
            local = [repo.parent / f"laptop-wt-{n}" for n in range(3)]
            for n, wt in enumerate(local):
                git(repo, "worktree", "add", "-q", "-b", f"laptop-{n}", str(wt))
            follower = start_follower(laptop, repo, tmp_path / "follower.log")
            # The box: three clones (three caches) behind the proxy, no follower.
            remote_clones = []
            for n in range(3):
                clone = tmp_path / f"box-clone-{n}"
                git(tmp_path, "clone", "-q", str(origin), str(clone))
                remote_clones.append(clone)

            # Without the proxy's headers the box gets the proxy's answer, never the
            # server's, and the client refuses it (SPEC §9.1).
            bare = server.client(
                tmp_path / "box-no-headers", machine="box", token=box.token, url=proxy.url
            )
            refused = lattice(
                bare, remote_clones[0], "create", "no", "--actor", "agent:x", "--json", check=False
            )
            assert refused.returncode != 0
            assert json.loads(refused.stdout)["error"]["code"] == "PROXY_REJECTED"
            assert proxy.refused and not proxy.admitted
            proxy.requests.clear()

            records, polls = run_writers(
                tmp_path,
                [(laptop, wt, f"agent:laptop-{n}") for n, wt in enumerate(local)]
                + [(box, clone, f"agent:box-{n}") for n, clone in enumerate(remote_clones)],
                [(laptop, [repo, *local]), (box, remote_clones)],
                tasks=3,
                writes=216,
            )
            dirs = [repo, *local, *remote_clones]
            worst = assert_fresh(records, polls, dirs)
            print(f"B worst visibility delay per directory: {worst}")
            for cwd in [repo, *local]:
                assert_matches_server(laptop, cwd, server)
            for cwd in remote_clones:
                assert_matches_server(box, cwd, server)
            for cwd in dirs:
                assert lattice_paths_in_status(cwd) == [], cwd
            # Every box request went through the proxy with its headers.
            assert proxy.admitted and not proxy.refused, proxy.refused[:3]
            assert all(r["has_authorization"] for r in proxy.admitted)
            # The laptop's view of the box's writes came from its follower.
            assert lattice_json(laptop, repo, "remote", "status")["follower"]["live"]
            assert stop_process(follower) == 0
            follower = None
            assert_doctor_clean(laptop, local[0], server)
            report = lattice_json(box, remote_clones[0], "doctor")
            assert not [f for f in report["findings"] if f["level"] == "error"], report
    finally:
        if follower is not None:
            stop_process(follower)
        server.stop()
        chmod_tree_writable(tmp_path)
