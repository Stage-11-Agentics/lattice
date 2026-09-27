"""Shared steps of the scenario rehearsals (AC-40, AC-41, AC-42): a repository whose
board is tracked in git, the move by the guide's steps (SPEC §11), scripted
clients, and the freshness measurement.

The move runs the steps ``lattice server project import --json`` prints
(``move_steps``), command by command, in the checkout, with ``<alias>`` filled
in: the rehearsal follows the guide the import hands the operator, not a copy
of it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from tests.torture.harness import (
    CLI_SHIM,
    PROJECT,
    REMOTE,
    Client,
    ServerProcess,
    base_env,
    git,
    lattice,
)

SCRIPTED = Path(__file__).with_name("scripted.py")
#: The freshness bound of AC-40 / AC-41: every write visible everywhere within 2 s.
FRESHNESS_SECONDS = 2.0


# ---------------------------------------------------------------------------
# A repository whose board is tracked in git
# ---------------------------------------------------------------------------


def tracked_board_repo(client: Client, base: Path, *, tasks: int = 3) -> tuple[Path, Path]:
    """A bare ``origin`` and a clone whose local board (code ``DEM``, *tasks* tasks,
    one with a plan and a comment) is committed and pushed on ``main``."""
    origin = base / "origin.git"
    git(base, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = base / "repo"
    git(base, "clone", "-q", str(origin), str(repo))
    git(repo, "checkout", "-q", "-b", "main")
    (repo / "README.md").write_text("repo\n")
    lattice(client, repo, "init", "--project-code", "DEM", "--actor", "human:alice")
    for n in range(tasks):
        lattice(client, repo, "create", f"Tracked {n}", "--actor", "human:alice")
    lattice(
        client,
        repo,
        "plan",
        "write",
        "DEM-1",
        "--stdin",
        "--actor",
        "human:alice",
        input="# DEM-1\n\nThe tracked plan.\n",
    )
    lattice(client, repo, "comment", "DEM-1", "before the move", "--actor", "human:alice")
    extras = [name for name in ("CLAUDE.md", "agents.md", "AGENTS.md") if (repo / name).exists()]
    git(repo, "add", "README.md", ".lattice", *extras)  # init's agent files ride along
    git(repo, "commit", "-q", "-m", "track the board")
    git(repo, "push", "-q", "-u", "origin", "main")
    return origin, repo


def cut_feature_branch(repo: Path, branch: str) -> None:
    """A pushed branch cut before the move: it still tracks ``.lattice/``."""
    git(repo, "checkout", "-q", "-b", branch)
    (repo / "feature.txt").write_text("feature work\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "feature work")
    git(repo, "push", "-q", "-u", "origin", branch)
    git(repo, "checkout", "-q", "main")


# ---------------------------------------------------------------------------
# The move (SPEC §11)
# ---------------------------------------------------------------------------


@dataclass
class Moved:
    imported: dict
    commands: list[str] = field(default_factory=list)
    status: dict = field(default_factory=dict)


def import_board(server: ServerProcess, repo: Path, work: Path) -> dict:
    """Guide step 2: import a copy of the board on the server host."""
    copy = work / "board-copy"
    shutil.copytree(repo / ".lattice", copy / ".lattice", symlinks=True)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            CLI_SHIM,
            "server",
            "project",
            "import",
            PROJECT,
            "--from",
            str(copy),
            "--root",
            str(server.root),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=base_env(),
    )
    envelope = json.loads(proc.stdout)
    assert proc.returncode == 0 and envelope["ok"], proc.stdout + proc.stderr
    return envelope["data"]


def move_board(server: ServerProcess, client: Client, repo: Path, work: Path) -> Moved:
    """Move *repo*'s tracked board onto *server* by the printed guide steps."""
    imported = import_board(server, repo, work)
    assert [step["step"] for step in imported["move_steps"]] == [1, 2, 3, 4, 5]
    moved = Moved(imported=imported)
    for step in imported["move_steps"][2:]:  # 1 (writers stopped) and 2 (import) are done
        for command in step["commands"]:
            command = command.replace("<alias>", REMOTE)
            moved.commands.append(command)
            if command.startswith("lattice "):
                args = command.split()[1:]
                if args[:2] == ["remote", "status"]:
                    args.append("--json")
                proc = lattice(client, repo, *args)
                if args[:2] == ["remote", "status"]:
                    moved.status = json.loads(proc.stdout)["data"]
            else:
                proc = subprocess.run(
                    ["bash", "-c", command],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={**client.env, **_git_identity()},
                )
                assert proc.returncode == 0, f"{command}: {proc.stdout}{proc.stderr}"
    return moved


def _git_identity() -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": "Torture",
        "GIT_AUTHOR_EMAIL": "torture@example.com",
        "GIT_COMMITTER_NAME": "Torture",
        "GIT_COMMITTER_EMAIL": "torture@example.com",
    }


# ---------------------------------------------------------------------------
# Scripted clients and freshness
# ---------------------------------------------------------------------------


def start_scripted(client: Client, spec: dict, path: Path) -> subprocess.Popen:
    """Start ``scripted.py`` for *client* with *spec* (saved at *path*.json)."""
    spec_path = path.with_suffix(".json")
    spec_path.write_text(json.dumps(spec))
    log = open(path.with_suffix(".log"), "wb")  # noqa: SIM115 - the child owns it
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPTED), str(spec_path)],
        env=client.env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    log.close()
    return proc


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def wait_all(procs: list[subprocess.Popen], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    for proc in procs:
        proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        assert proc.returncode == 0, f"scripted client exited {proc.returncode}"


def freshness(writes: list[dict], polls: list[dict]) -> dict[str, float]:
    """For each directory polled, the longest a write stayed invisible there: the
    greatest delay between a write's acknowledgement and the start of a ``list``
    that still did not show it (0 when every ``list`` started after the
    acknowledgement showed it). A write no ``list`` there ever showed is ``inf``.

    So "every write visible within 2 s" is ``freshness(...)[cwd] <= 2`` for every
    directory: any read that began 2 s after a write saw it. Measuring from the
    start of the read keeps a slow poll cycle (two cores, a dozen processes) from
    counting as staleness.

    A task's writes come from one writer in order, so a ``list`` showing a task's
    *k*-th ``last_event_id`` shows every earlier write to it too.
    """
    order: dict[str, list[str]] = {}
    acked: dict[str, list[float]] = {}
    for record in sorted(writes, key=lambda r: r["t"]):
        if "last_event_id" not in record:
            continue
        task, event = record["task"], record["last_event_id"]
        if order.setdefault(task, [])[-1:] == [event]:
            continue  # an idempotent write: nothing new to see
        order[task].append(event)
        acked.setdefault(task, []).append(record["t"])
    by_cwd: dict[str, list[dict]] = {}
    for poll in polls:
        if "tasks" in poll:
            by_cwd.setdefault(poll["cwd"], []).append(poll)
    worst: dict[str, float] = {}
    for cwd, rows in by_cwd.items():
        stale = 0.0
        for task, events in order.items():
            index = {event: n for n, event in enumerate(events)}
            shown = [(row["t0"], index.get(row["tasks"].get(task), -1)) for row in rows]
            for j, t_ack in enumerate(acked[task]):
                if not any(k >= j for _, k in shown):
                    stale = float("inf")
                    continue
                missed = [t0 - t_ack for t0, k in shown if k < j and t0 > t_ack]
                stale = max(stale, *missed, 0.0)
        worst[cwd] = stale
    return worst
