"""Short-ID floor from the event logs (SPEC §5, AC-2 local part).

A short ID that appears in any task log, active or archived, is never issued
to another task, even when ``ids.json`` is regressed or deleted, and even when
the ID arrived by an event appended to an existing log (which changes no
directory entry, so a directory-mtime cache would miss it).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config, serialize_config
from lattice.core.events import create_event, serialize_event
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
from lattice.storage.short_ids import (
    allocate_short_id,
    load_id_index,
    max_observed_short_ids,
    next_short_id,
    save_id_index,
    short_ids_in_log,
)


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    ensure_lattice_dirs(tmp_path)
    lattice_dir = tmp_path / LATTICE_DIR
    config = dict(default_config())
    config["project_code"] = "LAT"
    config["auto_code_review_on_transition"] = False
    config["auto_plan_review_on_transition"] = False
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    save_id_index(lattice_dir, {"schema_version": 2, "next_seqs": {}, "map": {}})
    (lattice_dir / "events" / "_lifecycle.jsonl").touch()
    return tmp_path


def _run(root: Path, *args: str) -> dict:
    result = CliRunner().invoke(cli, [*args, "--json"], env={"LATTICE_ROOT": str(root)})
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _create(root: Path, title: str) -> dict:
    return _run(root, "create", title, "--actor", "human:test")


class _Reserved(Exception):
    pass


def _reserve(lattice_dir: Path, task_id: str, **kwargs: object) -> str:
    """Run a create's allocation step through ``mutate_task`` and return the ID."""
    from lattice.storage.operations import mutate_task

    def capture(context):  # noqa: ANN001, ANN202
        raise _Reserved(context.reserved_short_id)

    with pytest.raises(_Reserved) as caught:
        mutate_task(
            lattice_dir,
            task_id,
            capture,
            source="absent",
            project_prefix="LAT",
            run_hooks=False,
            **kwargs,
        )
    return caught.value.args[0]


def _regress_ids(root: Path, index: dict) -> None:
    save_id_index(root / LATTICE_DIR, index)


class TestLocalFloor:
    def test_regressed_ids_json_never_reissues_a_logged_id(self, board: Path) -> None:
        tasks = [_create(board, f"Task {n}") for n in range(1, 4)]
        assert [t["short_id"] for t in tasks] == ["LAT-1", "LAT-2", "LAT-3"]
        # A restore from an old backup: only LAT-1 known, counter back at 2.
        _regress_ids(
            board,
            {"schema_version": 2, "next_seqs": {"LAT": 2}, "map": {"LAT-1": tasks[0]["id"]}},
        )

        assert _create(board, "After regression")["short_id"] == "LAT-4"
        index = load_id_index(board / LATTICE_DIR)
        assert index["next_seqs"]["LAT"] == 5

    def test_deleted_ids_json_never_reissues_a_logged_id(self, board: Path) -> None:
        for n in range(1, 4):
            _create(board, f"Task {n}")
        (board / LATTICE_DIR / "ids.json").unlink()

        assert _create(board, "After deletion")["short_id"] == "LAT-4"

    def test_archived_logs_count(self, board: Path) -> None:
        tasks = [_create(board, f"Task {n}") for n in range(1, 4)]
        _run(board, "archive", tasks[2]["id"], "--actor", "human:test")
        assert (board / LATTICE_DIR / "archive" / "events" / f"{tasks[2]['id']}.jsonl").exists()
        _regress_ids(board, {"schema_version": 2, "next_seqs": {}, "map": {}})

        assert _create(board, "After archive")["short_id"] == "LAT-4"

    def test_appended_assignment_changes_no_directory_entry_and_is_never_reissued(
        self, board: Path
    ) -> None:
        tasks = [_create(board, f"Task {n}") for n in range(1, 3)]
        lattice_dir = board / LATTICE_DIR
        events_dir = lattice_dir / "events"
        log = events_dir / f"{tasks[0]['id']}.jsonl"
        # ids.json knows only LAT-1 and LAT-2, and its counter says LAT-3 is next.
        before_index = load_id_index(lattice_dir)
        assert before_index["next_seqs"]["LAT"] == 3

        dir_mtime = events_dir.stat().st_mtime_ns
        dir_entries = sorted(os.listdir(events_dir))
        # Another writer (a synced checkout, an older client) assigns LAT-3 by
        # appending to an existing log; no directory entry changes.
        event = create_event(
            type="task_short_id_assigned",
            task_id=tasks[0]["id"],
            actor="human:test",
            data={"short_id": "LAT-3"},
        )
        with log.open("a", encoding="utf-8") as handle:
            handle.write(serialize_event(event))
        assert events_dir.stat().st_mtime_ns == dir_mtime
        assert sorted(os.listdir(events_dir)) == dir_entries
        _regress_ids(board, before_index)

        assert _create(board, "After append")["short_id"] == "LAT-4"

    def test_counter_ahead_of_logs_is_kept(self, board: Path) -> None:
        _create(board, "Task 1")
        _regress_ids(board, {"schema_version": 2, "next_seqs": {"LAT": 10}, "map": {}})
        assert _create(board, "Counter ahead")["short_id"] == "LAT-10"

    def test_map_collisions_above_the_floor_are_skipped(self, board: Path) -> None:
        _create(board, "Task 1")
        _regress_ids(
            board,
            {
                "schema_version": 2,
                "next_seqs": {"LAT": 1},
                "map": {"LAT-2": "task_01RESERVEDXXXXXXXXXXXXXXXX"},
            },
        )
        assert _create(board, "Skips map")["short_id"] == "LAT-3"

    def test_reservation_already_held_by_another_log_is_not_reused(self, board: Path) -> None:
        first = _create(board, "Task 1")
        lattice_dir = board / LATTICE_DIR
        new_id = "task_01KZZZZZZZZZZZZZZZZZZZZZZZ"
        # A regressed index that reserves the logged LAT-1 for a new task.
        _regress_ids(board, {"schema_version": 2, "next_seqs": {}, "map": {"LAT-1": new_id}})

        assert first["short_id"] == "LAT-1"
        assert _reserve(lattice_dir, new_id) == "LAT-2"


class TestFloorPrimitives:
    def test_max_observed_reads_created_and_assigned_events_only(self, board: Path) -> None:
        task = _create(board, "Task 1")
        log = board / LATTICE_DIR / "events" / f"{task['id']}.jsonl"
        noise = [
            create_event(
                "comment_added", task["id"], "human:test", {"body": '"short_id":"LAT-90"'}
            ),
            create_event("x_custom", task["id"], "human:test", {"short_id": "LAT-91"}),
            create_event(
                "task_short_id_assigned", task["id"], "human:test", {"short_id": "OTH-7"}
            ),
        ]
        with log.open("a", encoding="utf-8") as handle:
            for event in noise:
                handle.write(serialize_event(event))
            handle.write('{"torn": "short_id"')  # an incomplete final line is skipped

        assert max_observed_short_ids(board / LATTICE_DIR) == {"LAT": 1, "OTH": 7}

    def test_short_ids_in_log_on_empty_and_plain_logs(self) -> None:
        assert short_ids_in_log(b"") == []
        assert short_ids_in_log(b'{"type":"task_created","data":{"title":"x"}}\n') == []

    def test_next_short_id_takes_a_caller_supplied_floor(self) -> None:
        index = {"schema_version": 2, "next_seqs": {"LAT": 3}, "map": {"LAT-9": "task_b"}}
        assert next_short_id(index, "LAT", "task_a", {"LAT": 7}) == "LAT-8"
        assert next_short_id(index, "LAT", "task_c", {"LAT": 7}) == "LAT-10"
        assert index["next_seqs"]["LAT"] == 11
        assert index["map"]["LAT-8"] == "task_a"
        assert next_short_id(index, "NEW", "task_d", {}) == "NEW-1"

    def test_mutate_task_uses_the_supplied_floor_without_rescanning(
        self, board: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lattice.storage import operations

        def no_scan(_lattice_dir: Path) -> dict[str, int]:
            raise AssertionError("a supplied floor must not rescan the logs")

        monkeypatch.setattr(operations, "max_observed_short_ids", no_scan)
        reserved = _reserve(
            board / LATTICE_DIR, "task_01KYYYYYYYYYYYYYYYYYYYYYYY", short_id_floor={"LAT": 41}
        )
        assert reserved == "LAT-42"

    def test_allocate_short_id_respects_the_log_floor(self, board: Path) -> None:
        for n in range(1, 3):
            _create(board, f"Task {n}")
        (board / LATTICE_DIR / "ids.json").unlink()
        short_id, index = allocate_short_id(board / LATTICE_DIR, "LAT")
        assert short_id == "LAT-3"
        assert "LAT-3" not in index["map"]
