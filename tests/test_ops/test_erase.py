"""AC-27 (local): ``erase`` and ``unerase`` as reversible tombstones (SPEC §7).

Erasing appends ``task_tombstoned`` and removes nothing; the task leaves
``list``, ``next``, and stats, stays reachable through ``show`` and
``list --include-tombstoned``, and refuses every other write with
``TASK_ERASED``. ``unerase`` brings it back, in its prior status, with both
events kept.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.boards import resolve_board
from lattice.cli.main import cli
from lattice.core.tasks import apply_event_to_snapshot, compact_snapshot
from lattice.core.ids import generate_op_id
from lattice.ops import Caller, OpError, execute

ACTOR = ("--actor", "human:test")


@pytest.fixture()
def run(cli_runner: CliRunner, cli_env: dict[str, str]):
    def _run(*args: str, env: dict[str, str] | None = None):
        return cli_runner.invoke(cli, list(args), env={**cli_env, **(env or {})})

    return _run


def _json(result) -> dict:  # noqa: ANN001
    return json.loads(result.output)


@pytest.fixture()
def board(initialized_root: Path, fill_plan, run) -> dict:  # noqa: ANN001
    """A task in ``planned`` with a comment, a reaction, a criterion, links, and a
    relationship, plus a second task; returns their ids and the comment's."""
    target = _json(run("create", "Target", *ACTOR, "--json"))["data"]["id"]
    other = _json(run("create", "Other", *ACTOR, "--json"))["data"]["id"]
    fill_plan(target, "Target")
    assert run("status", target, "planned", *ACTOR).exit_code == 0
    comment = _json(run("comment", target, "hello", *ACTOR, "--json"))["data"]["last_event_id"]
    assert run("react", target, comment, "thumbsup", *ACTOR).exit_code == 0
    assert run("criterion", "add", target, "It works", *ACTOR).exit_code == 0
    assert run("branch-link", target, "feat/x", *ACTOR).exit_code == 0
    assert run("file-link", target, "src/a.py", *ACTOR).exit_code == 0
    assert run("link", target, "blocks", other, *ACTOR).exit_code == 0
    return {"target": target, "other": other, "comment": comment, "root": initialized_root}


def _files(root: Path) -> dict[str, bytes]:
    """Every file under the board except lock files."""
    lattice = root / ".lattice"
    return {
        str(p.relative_to(lattice)): p.read_bytes()
        for p in sorted(lattice.rglob("*"))
        if p.is_file() and "locks" not in p.relative_to(lattice).parts
    }


def _events(root: Path, task_id: str) -> list[dict]:
    path = root / ".lattice" / "events" / f"{task_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _execute(root: Path, op: str, params: dict, calls: list[tuple[str, str]]):  # noqa: ANN202
    """Run *op* through ``execute`` with a write recorder (SPEC §8.5) that logs
    every durable mutation's file name and kind into *calls*."""
    return execute(
        root / ".lattice",
        op,
        params,
        Caller(actor="human:test", origin={"op_id": generate_op_id()}),
        run_hooks=True,
        on_mutation=lambda path, kind: calls.append((path.name, kind)),
    )


def _listed(run, *extra: str) -> list[str]:  # noqa: ANN001
    return [t["id"] for t in _json(run("list", "--json", *extra))["data"]]


# ---------------------------------------------------------------------------
# erase
# ---------------------------------------------------------------------------


def test_erase_appends_a_tombstone_and_removes_nothing(board: dict, run) -> None:  # noqa: ANN001
    root, target = board["root"], board["target"]
    before = _files(root)
    calls: list[tuple[str, str]] = []
    result = _execute(root, "task.erase", {"task": target, "reason": "duplicate of Other"}, calls)
    snap = result.value
    assert snap["tombstoned"] is True
    assert snap["tombstone_reason"] == "duplicate of Other"
    assert snap["tombstoned_at"] == _events(root, target)[-1]["ts"]
    assert snap["status"] == "planned"

    last = _events(root, target)[-1]
    assert last["type"] == "task_tombstoned"
    assert last["data"] == {"reason": "duplicate of Other"}

    # The recorder saw no removal, and every file that existed still does,
    # its old bytes intact (the event log grew by exactly one line).
    assert [kind for _name, kind in calls if kind == "unlink"] == []
    assert (f"{target}.jsonl", "append") in calls
    assert set(result.paths) == {f"events/{target}.jsonl", f"tasks/{target}.json"}
    after = _files(root)
    assert set(before) <= set(after)
    log = f"events/{target}.jsonl"
    assert after[log].startswith(before[log])
    assert after[log].count(b"\n") == before[log].count(b"\n") + 1
    for path, content in before.items():
        if path not in (log, f"tasks/{target}.json"):
            assert after[path] == content, path


def test_erased_task_leaves_list_next_and_stats(board: dict, run) -> None:  # noqa: ANN001
    target, other = board["target"], board["other"]
    stats_before = _json(run("stats", "--json"))["data"]["summary"]
    assert run("erase", target, "--reason", "gone", *ACTOR).exit_code == 0

    assert _listed(run) == [other]
    assert target not in run("list").output
    assert target not in run("list", "--quiet").output
    assert _listed(run, "--include-tombstoned") == [target, other]
    plain = run("list", "--include-tombstoned").output
    assert "[ERASED]" in plain.splitlines()[0]
    compact = _json(run("list", "--json", "--compact", "--include-tombstoned"))["data"]
    assert compact[0]["tombstoned"] is True and "tombstoned" not in compact[1]

    # next: the erased task is in `planned` (a ready status) but never picked.
    picked = _json(run("next", "--json"))["data"]
    assert picked["id"] == other
    assert _json(run("next", "--json", "--status", "planned"))["data"] is None

    stats = _json(run("stats", "--json"))["data"]["summary"]
    assert stats["active_tasks"] == stats_before["active_tasks"] - 1
    assert stats["total_events"] < stats_before["total_events"]


def test_show_still_finds_an_erased_task(board: dict, run) -> None:  # noqa: ANN001
    target = board["target"]
    run("erase", target, "--reason", "wrong board", *ACTOR)
    plain = run("show", target)
    assert plain.exit_code == 0
    assert "ERASED: wrong board" in plain.output.splitlines()[:2]
    assert "ERASED: wrong board" in run("show", target, "--compact").output
    data = _json(run("show", target, "--json"))["data"]
    assert data["tombstoned"] is True and data["tombstone_reason"] == "wrong board"


def test_erase_requires_a_reason(board: dict, run) -> None:  # noqa: ANN001
    before = _files(board["root"])
    for extra in ((), ("--reason", "  ")):
        result = run("erase", board["target"], *extra, *ACTOR, "--json")
        assert result.exit_code == 1
        assert _json(result)["error"] == {
            "code": "VALIDATION_ERROR",
            "message": "--reason is required.",
        }
    assert _files(board["root"]) == before


# Every task-writing command, aimed at the erased task. Commands not yet
# converted to operations meet TASK_ERASED in the write path itself.
REFUSED_WRITES = [
    ("erase", "{t}", "--reason", "again"),
    ("update", "{t}", "priority=high"),
    ("edit-description", "{t}", "new text"),
    ("status", "{t}", "in_progress"),
    ("assign", "{t}", "agent:x"),
    ("needs-human", "{t}", "help"),
    ("comment", "{t}", "more"),
    ("comment-edit", "{t}", "{c}", "edited"),
    ("comment-delete", "{t}", "{c}"),
    ("react", "{t}", "{c}", "tada"),
    ("unreact", "{t}", "{c}", "thumbsup"),
    ("complete", "{t}", "--review", "LGTM"),
    ("link", "{t}", "related_to", "{o}"),
    ("unlink", "{t}", "blocks", "{o}"),
    ("branch-link", "{t}", "feat/y"),
    ("branch-unlink", "{t}", "feat/x"),
    ("file-link", "{t}", "src/b.py"),
    ("file-unlink", "{t}", "src/a.py"),
    ("criterion", "add", "{t}", "Another"),
    ("criterion", "edit", "{t}", "AC-1", "Revised"),
    ("criterion", "retire", "{t}", "AC-1"),
    ("archive", "{t}"),
    ("claim", "{t}"),
    ("unclaim", "{t}"),
    ("attach", "{t}", "https://example.com/x", "--title", "x"),
    ("event", "{t}", "x_custom"),
]


@pytest.mark.parametrize("args", REFUSED_WRITES, ids=lambda a: " ".join(a[:2]).replace("{t}", ""))
def test_every_other_write_is_refused(board: dict, run, args: tuple[str, ...]) -> None:  # noqa: ANN001
    root, target = board["root"], board["target"]
    run("erase", target, "--reason", "gone", *ACTOR)
    before = _files(root)
    argv = [a.format(t=target, o=board["other"], c=board["comment"]) for a in args]
    for is_json in (False, True):
        result = run(
            *argv, *ACTOR, *(("--json",) if is_json else ()), env={"C11_SURFACE_ID": "s-1"}
        )
        assert result.exit_code == 1, result.output
        if is_json:
            assert _json(result)["error"]["code"] == "TASK_ERASED"
        else:
            assert "is erased (gone)" in result.output
    assert _files(root) == before


def test_task_erased_carries_the_snapshot(board: dict) -> None:
    target = board["target"]
    local = resolve_board(board["root"])
    caller = Caller(actor="human:test")
    local.execute("task.erase", {"task": target, "reason": "gone"}, caller)
    with pytest.raises(OpError) as exc:
        local.execute("task.comment", {"task": target, "text": "hi"}, caller)
    assert exc.value.code == "TASK_ERASED"
    assert exc.value.http_status == 422
    snap = exc.value.details["snapshot"]
    assert snap["id"] == target and snap["tombstoned"] is True
    assert snap["last_event_id"] == _events(board["root"], target)[-1]["id"]


# ---------------------------------------------------------------------------
# unerase
# ---------------------------------------------------------------------------


def test_unerase_restores_every_view_in_the_prior_status(board: dict, run) -> None:  # noqa: ANN001
    root, target, other = board["root"], board["target"], board["other"]
    listed = _listed(run)
    shown = _json(run("show", target, "--json"))["data"]
    stats = _json(run("stats", "--json"))["data"]["summary"]
    run("erase", target, "--reason", "oops", *ACTOR)
    before = _files(root)

    calls: list[tuple[str, str]] = []
    result = _execute(root, "task.unerase", {"task": target, "reason": "erased by mistake"}, calls)
    assert [kind for _name, kind in calls if kind == "unlink"] == []
    assert (f"{target}.jsonl", "append") in calls
    after = _files(root)
    assert set(before) <= set(after)
    log = f"events/{target}.jsonl"
    assert after[log].startswith(before[log])

    snap = result.value
    for key in ("tombstoned", "tombstoned_at", "tombstone_reason"):
        assert key not in snap
    assert snap["status"] == "planned"

    types = [e["type"] for e in _events(root, target)]
    assert types[-2:] == ["task_tombstoned", "task_untombstoned"]
    assert _events(root, target)[-1]["data"] == {"reason": "erased by mistake"}

    assert _listed(run) == listed
    assert _json(run("next", "--json", "--status", "planned"))["data"]["id"] == target
    assert (
        _json(run("stats", "--json"))["data"]["summary"]["active_tasks"] == stats["active_tasks"]
    )
    now = _json(run("show", target, "--json"))["data"]
    ignore = {"last_event_id", "updated_at", "events"}
    assert {k: v for k, v in now.items() if k not in ignore} == {
        k: v for k, v in shown.items() if k not in ignore
    }
    assert "ERASED" not in run("show", target).output
    # Writes work again.
    assert run("comment", target, "back", *ACTOR).exit_code == 0
    assert other in _listed(run)


def test_unerase_of_a_task_that_is_not_erased(board: dict, run) -> None:  # noqa: ANN001
    before = _files(board["root"])
    result = run("unerase", board["target"], "--reason", "x", *ACTOR, "--json")
    assert result.exit_code == 1
    assert _json(result)["error"]["code"] == "CONFLICT"
    assert _files(board["root"]) == before

    with pytest.raises(OpError) as exc:
        resolve_board(board["root"]).execute(
            "task.unerase", {"task": board["target"], "reason": "x"}, Caller(actor="human:t")
        )
    assert exc.value.details["snapshot"]["id"] == board["target"]


def test_erase_and_unerase_cycle(board: dict, run) -> None:  # noqa: ANN001
    target = board["target"]
    for n in range(2):
        assert run("erase", target, "--reason", f"e{n}", *ACTOR).exit_code == 0
        assert run("unerase", target, "--reason", f"u{n}", *ACTOR).exit_code == 0
    types = [e["type"] for e in _events(board["root"], target)]
    assert types[-4:] == ["task_tombstoned", "task_untombstoned"] * 2
    # A rebuild replays the history to the same snapshot.
    shown = _json(run("show", target, "--json"))["data"]
    assert run("rebuild", target).exit_code == 0
    assert _json(run("show", target, "--json"))["data"] == shown


def test_rebuild_and_doctor_accept_an_erased_task(board: dict, run) -> None:  # noqa: ANN001
    target = board["target"]
    run("erase", target, "--reason", "gone", *ACTOR)
    snapshot_path = board["root"] / ".lattice" / "tasks" / f"{target}.json"
    expected = snapshot_path.read_bytes()
    snapshot_path.write_text("{}")
    assert run("rebuild", target).exit_code == 0
    assert snapshot_path.read_bytes() == expected
    assert run("doctor").exit_code == 0


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------


def _ev(type_: str, data: dict, n: int) -> dict:
    return {
        "id": f"ev_{n}",
        "type": type_,
        "task_id": "task_x",
        "actor": "human:t",
        "ts": f"2026-01-0{n}T00:00:00Z",
        "data": data,
    }


def test_reducers_add_and_remove_the_tombstone_fields() -> None:
    snap = apply_event_to_snapshot(
        None, _ev("task_created", {"title": "t", "status": "backlog"}, 1)
    )
    erased = apply_event_to_snapshot(snap, _ev("task_tombstoned", {"reason": "r"}, 2))
    assert erased["tombstoned"] is True
    assert erased["tombstoned_at"] == "2026-01-02T00:00:00Z"
    assert erased["tombstone_reason"] == "r"
    assert compact_snapshot(erased)["tombstoned"] is True
    restored = apply_event_to_snapshot(erased, _ev("task_untombstoned", {"reason": "u"}, 3))
    for key in ("tombstoned", "tombstoned_at", "tombstone_reason"):
        assert key not in restored and key not in snap
    assert "tombstoned" not in compact_snapshot(restored)
    assert restored["status"] == "backlog"
