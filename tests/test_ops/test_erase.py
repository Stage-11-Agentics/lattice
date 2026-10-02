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
from lattice.storage.operations import AuthoritativeLogError, read_task_authority

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
    ("attach", "{t}", "{r}/evidence.txt"),
    ("event", "{t}", "x_custom"),
    # Composite commands refuse before spending an agent on the task.
    ("code-review", "{t}"),
    ("plan-review", "{t}"),
]


@pytest.mark.parametrize("args", REFUSED_WRITES, ids=lambda a: " ".join(a[:2]).replace("{t}", ""))
def test_every_other_write_is_refused(board: dict, run, args: tuple[str, ...]) -> None:  # noqa: ANN001
    root, target = board["root"], board["target"]
    run("erase", target, "--reason", "gone", *ACTOR)
    (root / "evidence.txt").write_text("proof\n")
    before = _files(root)
    argv = [a.format(t=target, o=board["other"], c=board["comment"], r=root) for a in args]
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


# ---------------------------------------------------------------------------
# Review round 1: protected tombstone fields, backfill-ids, stats and weather
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["tombstoned", "tombstoned_at", "tombstone_reason"])
def test_field_updated_cannot_forge_a_tombstone_field(board: dict, run, field: str) -> None:  # noqa: ANN001
    """The tombstone fields belong to the tombstone events alone: strict replay
    rejects a field_updated that names one, erased or not."""
    root, target = board["root"], board["target"]
    result = run("update", target, f"{field}=ghost", *ACTOR, "--json")
    assert result.exit_code == 1 and _json(result)["ok"] is False

    log = root / ".lattice" / "events" / f"{target}.jsonl"
    forged = {
        "schema_version": 1,
        "id": "ev_01J9ZABCDEFGHJKMNPQRSTVWXY",
        "ts": "2026-09-27T00:00:00Z",
        "type": "field_updated",
        "task_id": target,
        "actor": "human:test",
        "data": {"field": field, "to": "ghost"},
    }
    with log.open("a") as f:
        f.write(json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(AuthoritativeLogError, match="protected field"):
        read_task_authority(root / ".lattice", target)


def test_backfill_ids_skips_an_erased_task_and_reports_it(
    cli_runner: CliRunner, cli_env: dict[str, str], initialized_root: Path
) -> None:
    def run(*args: str):  # noqa: ANN202
        return cli_runner.invoke(cli, list(args), env=cli_env)

    erased = _json(run("create", "Erased", *ACTOR, "--json"))["data"]["id"]
    kept = _json(run("create", "Kept", *ACTOR, "--json"))["data"]["id"]
    assert "short_id" not in _json(run("show", kept, "--json"))["data"]
    assert run("erase", erased, "--reason", "noise", *ACTOR).exit_code == 0
    log = initialized_root / ".lattice" / "events" / f"{erased}.jsonl"
    before = log.read_bytes()

    result = run("backfill-ids", "--code", "BF", "--json")
    assert result.exit_code == 0, result.output
    assert _json(result)["data"] == {
        "assigned": 1,
        "first": "BF-1",
        "last": "BF-1",
        "skipped_erased": [erased],
    }
    assert log.read_bytes() == before
    assert _json(run("show", kept, "--json"))["data"]["short_id"] == "BF-1"

    plain = run("backfill-ids")
    assert plain.exit_code == 0
    assert plain.output.splitlines() == [
        f"Skipped 1 erased task(s): {erased}. Unerase them, then run backfill-ids again."
    ]
    # After unerase, the same command assigns the next ID.
    assert run("unerase", erased, "--reason", "back", *ACTOR).exit_code == 0
    again = _json(run("backfill-ids", "--json"))["data"]
    assert again == {"assigned": 1, "first": "BF-2", "last": "BF-2"}


def test_rebuild_exemption_cannot_append_to_an_erased_task(board: dict) -> None:
    """``allow_tombstoned`` admits only ``task_untombstoned``."""
    from lattice.core.errors import TaskErased
    from lattice.core.events import create_event
    from lattice.storage.operations import TaskMutationDecision, mutate_task

    root, target = board["root"], board["target"]
    resolve_board(root).execute(
        "task.erase", {"task": target, "reason": "gone"}, Caller(actor="human:test")
    )
    before = _files(root)
    event = create_event("comment_added", target, "human:test", {"body": "sneak"})
    with pytest.raises(TaskErased):
        mutate_task(
            root / ".lattice",
            target,
            lambda _c: TaskMutationDecision(events=[event]),
            run_hooks=False,
            allow_tombstoned=True,
        )
    assert _files(root) == before


def test_stats_visibility_is_explicit(board: dict, run) -> None:  # noqa: ANN001
    """``lattice stats`` hides erased tasks; ``build_stats``' default (the
    dashboard's call, until H-13a) still counts them."""
    from lattice.core.config import default_config
    from lattice.core.stats import build_stats

    root = board["root"]
    run("erase", board["target"], "--reason", "gone", *ACTOR)
    lattice_dir = root / ".lattice"
    config = default_config()
    assert build_stats(lattice_dir, config)["summary"]["active_tasks"] == 2
    hidden = build_stats(lattice_dir, config, include_tombstoned=False)
    assert hidden["summary"]["active_tasks"] == 1
    assert _json(run("stats", "--json"))["data"]["summary"] == hidden["summary"]


def test_weather_never_shows_an_erased_task(
    cli_runner: CliRunner, cli_env: dict[str, str], initialized_root: Path, fill_plan
) -> None:  # noqa: ANN001
    def run(*args: str):  # noqa: ANN202
        return cli_runner.invoke(cli, list(args), env=cli_env)

    erased = _json(run("create", "Ghost planned task", *ACTOR, "--json"))["data"]["id"]
    fill_plan(erased, "Ghost")
    assert run("status", erased, "planned", *ACTOR).exit_code == 0
    assert run("erase", erased, "--reason", "gone", *ACTOR).exit_code == 0

    data = _json(run("weather", "--json"))["data"]
    assert data["vital_signs"]["active_tasks"] == 0
    assert data["vital_signs"]["events_24h"] == 0
    assert data["up_next"] == [] and data["attention"] == []
    for args in (("weather",), ("weather", "--markdown"), ("weather", "--json")):
        output = run(*args).output
        assert erased not in output and "Ghost planned task" not in output

    # Unerased, the same task is back in both the counts and the lists.
    assert run("unerase", erased, "--reason", "back", *ACTOR).exit_code == 0
    data = _json(run("weather", "--json"))["data"]
    assert data["vital_signs"]["active_tasks"] == 1
    assert [t["title"] for t in data["up_next"]] == ["Ghost planned task"]


# ---------------------------------------------------------------------------
# Round 3: `next` / `next --claim` (board.next_claim, H-2) skip erased tasks
# ---------------------------------------------------------------------------


@pytest.fixture()
def ready(run, fill_plan):  # noqa: ANN001
    """Create a backlog task with a real plan (the claim's plan gate passes)."""

    def _ready(title: str, priority: str = "medium") -> str:
        task_id = _json(run("create", title, "--priority", priority, *ACTOR, "--json"))["data"][
            "id"
        ]
        fill_plan(task_id, title)
        return task_id

    return _ready


@pytest.mark.parametrize("is_json", [False, True], ids=["plain", "json"])
def test_next_claim_skips_an_erased_best_candidate(
    initialized_root: Path, run, ready, is_json: bool
) -> None:  # noqa: ANN001
    best = ready("Erased best", "critical")
    second = ready("Visible second", "high")
    run("erase", best, "--reason", "gone", *ACTOR)
    erased_log = initialized_root / ".lattice" / "events" / f"{best}.jsonl"
    before = erased_log.read_bytes()

    json_flag = ("--json",) if is_json else ()
    shown = run("next", *json_flag)
    claimed = run("next", "--claim", "--actor", "agent:w", *json_flag)
    assert claimed.exit_code == 0, claimed.output
    if is_json:
        assert _json(shown)["data"]["id"] == second
        data = _json(claimed)["data"]
        assert data["id"] == second
        assert data["status"] == "in_planning" and data["assigned_to"] == "agent:w"
    else:
        assert "Visible second" in shown.output and "Erased best" not in shown.output
        assert "Visible second" in claimed.output and "Erased best" not in claimed.output
    assert erased_log.read_bytes() == before

    # With only the erased task left in the pool, there is nothing to claim.
    again = run("next", "--claim", "--actor", "agent:v", "--json")
    assert again.exit_code == 0 and _json(again)["data"] is None
    assert erased_log.read_bytes() == before


def test_concurrent_next_claims_skip_an_erased_task(initialized_root: Path, run, ready) -> None:  # noqa: ANN001
    import threading

    best = ready("Erased best", "critical")
    visible_tasks = {ready(f"t{i}") for i in range(2)}
    run("erase", best, "--reason", "gone", *ACTOR)
    local = resolve_board(initialized_root)
    for _ in range(5):
        barrier = threading.Barrier(2)
        results: list = [None, None]
        errors: list[BaseException] = []

        def claim(i: int, actor: str) -> None:
            try:
                barrier.wait()
                results[i] = local.execute("board.next_claim", {}, Caller(actor=actor)).value
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [
            threading.Thread(target=claim, args=(i, a))
            for i, a in enumerate(["agent:a", "agent:b"])
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors, errors
        claimed = {r["id"] for r in results}
        assert claimed == visible_tasks  # two distinct tasks, never the erased one
        for task_id in visible_tasks:  # back to the pool for the next round
            local.execute(
                "task.assign", {"task": task_id, "actor_id": "none"}, Caller(actor="agent:t")
            )
            local.execute(
                "task.status",
                {"task": task_id, "new_status": "backlog", "force": True, "reason": "r"},
                Caller(actor="agent:t"),
            )
    assert "tombstoned" in _json(run("show", best, "--json"))["data"]


def test_unarchive_of_an_erased_task_is_refused(board: dict, run) -> None:  # noqa: ANN001
    """An erased task cannot be archived through Lattice, so an archived erased
    task only comes from a hand-edited log; unarchive still refuses it."""
    root, target = board["root"], board["target"]
    run("erase", target, "--reason", "gone", *ACTOR)
    log = root / ".lattice" / "events" / f"{target}.jsonl"
    archived = {
        "schema_version": 1,
        "id": "ev_01J9ZABCDEFGHJKMNPQRSTVWXY",
        "ts": "2026-09-27T00:00:00Z",
        "type": "task_archived",
        "task_id": target,
        "actor": "human:test",
        "data": {},
    }
    with log.open("a") as f:
        f.write(json.dumps(archived, sort_keys=True, separators=(",", ":")) + "\n")
    assert run("rebuild", target).exit_code == 0
    assert (root / ".lattice" / "archive" / "events" / f"{target}.jsonl").exists()
    before = _files(root)
    for extra in ((), ("--json",)):
        result = run("unarchive", target, *ACTOR, *extra)
        assert result.exit_code == 1
        if extra:
            assert _json(result)["error"]["code"] == "TASK_ERASED"
        else:
            assert "is erased (gone)" in result.output
    assert _files(root) == before


H2_OPS = [
    ("task.update", {"pairs": ["priority=high"]}),
    ("task.edit_description", {"description": "new"}),
    ("task.assign", {"actor_id": "agent:x"}),
    ("task.needs_human", {"flag_reason": "help"}),
    ("task.claim", {"surface": "s-1"}),
    ("task.unclaim", {}),
    ("task.archive", {}),
    ("task.event", {"event_type": "x_custom"}),
]


@pytest.mark.parametrize(("op", "params"), H2_OPS, ids=[op for op, _ in H2_OPS])
def test_converted_operations_refuse_an_erased_task(board: dict, op: str, params: dict) -> None:
    """Every operation H-2 converted raises TASK_ERASED with the task snapshot
    and writes nothing (``task.unarchive``: see the test above)."""
    root, target = board["root"], board["target"]
    local = resolve_board(root)
    caller = Caller(actor="human:test")
    local.execute("task.erase", {"task": target, "reason": "gone"}, caller)
    before = _files(root)
    with pytest.raises(OpError) as exc:
        local.execute(op, {"task": target, **params}, caller)
    assert exc.value.code == "TASK_ERASED"
    assert exc.value.details["snapshot"]["id"] == target
    assert exc.value.details["snapshot"]["tombstoned"] is True
    assert _files(root) == before
