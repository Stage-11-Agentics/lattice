"""``lattice.storage.integrity``: doctor's board scan and the task-derived repair.

The doctor goldens were recorded from the CLI before the scan moved out of
``cli/integrity_cmds.py``; they prove ``lattice doctor`` prints the same bytes,
plain and ``--json``, on a board that trips most checks.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.events import create_event, serialize_event
from lattice.storage import integrity
from lattice.storage.integrity import (
    check_board,
    repair_task_derived_files,
)
from lattice.storage.locks import task_locks

GOLDEN = Path(__file__).parents[1] / "fixtures" / "doctor_golden"


def _cli(*args: str, root: Path):
    return CliRunner().invoke(cli, list(args), env={"LATTICE_ROOT": str(root)})


def _ok(*args: str, root: Path) -> dict:
    result = _cli(*args, "--json", root=root)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _board(tmp_path: Path) -> tuple[Path, list[str]]:
    src = tmp_path / "repo"
    src.mkdir()
    result = CliRunner().invoke(
        cli,
        ["init", "--path", str(src), "--project-code", "INT", "--actor", "human:t",
         "--preset", "stage11", "--no-setup-claude", "--no-setup-agents"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    ids = []
    for title in ("One", "Two", "Three", "Four"):
        time.sleep(0.003)  # distinct milliseconds, so ID order is creation order
        ids.append(_ok("create", title, "--actor", "human:t", root=src)["id"])
    assert ids == sorted(ids)
    return src, ids


def _findings_board(tmp_path: Path) -> Path:
    """A board that trips most of doctor's checks, errors and warnings both."""
    src, (one, two, three, four) = _board(tmp_path)
    board = src / ".lattice"
    _ok("link", one, "blocks", two, "--actor", "human:t", root=src)
    _ok("status", four, "done", "--force", "--reason", "fixture", "--actor", "human:t", root=src)
    _ok("resource", "create", "db", "--actor", "human:t", root=src)
    _ok("event", two, "x_note", "--data", '{"short_id": "INT-30"}', "--actor", "human:t", root=src)
    # snapshot drift
    snap = board / "tasks" / f"{one}.json"
    data = json.loads(snap.read_text())
    data["title"] = "drifted"
    snap.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    # an invalid middle line (error) and a truncated final line (warning)
    log = board / "events" / f"{two}.jsonl"
    lines = log.read_text().splitlines(keepends=True)
    log.write_text(lines[0] + "{broken\n" + "".join(lines[1:]))
    log3 = board / "events" / f"{three}.jsonl"
    log3.write_text(log3.read_text() + '{"trunc')
    # a relationship to a missing task and a lifecycle event missing from the global log
    snap4 = board / "tasks" / f"{four}.json"
    data4 = json.loads(snap4.read_text())
    data4["relationships_out"] = [{"type": "blocks", "target_task_id": "task_MISSING"}]
    snap4.write_text(json.dumps(data4, sort_keys=True, indent=2) + "\n")
    lifecycle = board / "events" / "_lifecycle.jsonl"
    lifecycle.write_text("".join(lifecycle.read_text().splitlines(keepends=True)[1:]))
    # a counter behind the logs and an ids.json entry for a task with no log
    ids = json.loads((board / "ids.json").read_text())
    ids["next_seqs"]["INT"] = 3
    ids["map"]["INT-99"] = "task_01ZZZZZZZZZZZZZZZZZZZZZZZZ"
    (board / "ids.json").write_text(json.dumps(ids, sort_keys=True, indent=2) + "\n")
    return src


_ID = re.compile(r"\b(task|ev|art|res|inst)_[0-9A-Za-z]{26}\b")


def _normalize(text: str, src: Path) -> str:
    seen: dict[str, str] = {}

    def sub(match: re.Match) -> str:
        return seen.setdefault(match.group(0), f"<{match.group(1)}:{len(seen) + 1}>")

    text = text.replace(str(src), "<SRC>").replace(str(src.resolve()), "<SRC>")
    return _ID.sub(sub, text)


@pytest.mark.parametrize("mode", ["plain", "json"])
def test_doctor_output_is_byte_identical_to_the_pre_extraction_golden(
    tmp_path: Path, mode: str
) -> None:
    src = _findings_board(tmp_path)
    args = ["doctor"] + (["--json"] if mode == "json" else [])
    result = CliRunner().invoke(cli, args, env={"LATTICE_ROOT": str(src)})
    observed = {"exit_code": result.exit_code, "output": _normalize(result.output, src)}
    golden = GOLDEN / f"findings.{mode}.json"
    if os.environ.get("RECORD_DOCTOR_GOLDEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(json.dumps(observed, indent=2) + "\n")
    assert observed == json.loads(golden.read_text())


def test_check_board_reports_what_the_cli_prints(tmp_path: Path) -> None:
    src = _findings_board(tmp_path)
    report = check_board(src / ".lattice")
    payload = json.loads(CliRunner().invoke(cli, ["doctor", "--json"], env={"LATTICE_ROOT": str(src)}).output)  # fmt: skip
    assert payload["data"]["summary"]["errors"] == report.errors > 0
    assert payload["data"]["summary"]["warnings"] == report.warnings > 0
    assert not report.jsonl_ok and not report.drift_ok and not report.alias_ok


def test_doctor_repair_holds_task_lock_while_trimming_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, (task_id, *_rest) = _board(tmp_path)
    lattice_dir = src / ".lattice"
    path = lattice_dir / "events" / f"{task_id}.jsonl"
    path.write_text(path.read_text() + '{"truncated"')
    appended = create_event("x_note", task_id, "human:t", {"note": "concurrent append"})
    writer_started = threading.Event()
    writer_finished = threading.Event()
    writers: list[threading.Thread] = []
    original_atomic_write = integrity.atomic_write

    def append_after_repair() -> None:
        writer_started.set()
        with task_locks(lattice_dir / "locks", [task_id]):
            with path.open("a") as stream:
                stream.write(serialize_event(appended))
        writer_finished.set()

    def pause_repair_write(target: Path, content: str | bytes) -> None:
        writer = threading.Thread(target=append_after_repair)
        writers.append(writer)
        writer.start()
        assert writer_started.wait(timeout=2)
        assert not writer_finished.wait(timeout=0.05)
        original_atomic_write(target, content)

    monkeypatch.setattr(integrity, "atomic_write", pause_repair_write)
    report = check_board(lattice_dir, fix=True)
    assert any("(fixed)" in finding["message"] for finding in report.findings)
    writers[0].join(timeout=2)
    assert not writers[0].is_alive()
    assert json.loads(path.read_text().splitlines()[-1]) == appended


def test_rebuild_all_raises_every_counter_to_the_log_floor(tmp_path: Path) -> None:
    """A historical short ID absent from the alias map still sets the floor (SPEC §5)."""
    src, (one, two, _three, _four) = _board(tmp_path)
    _ok("event", two, "x_note", "--data", '{"short_id": "INT-30"}', "--actor", "human:t", root=src)
    ids_path = src / ".lattice" / "ids.json"
    ids = json.loads(ids_path.read_text())
    assert "INT-30" not in ids["map"]
    ids["next_seqs"]["INT"] = 3
    ids_path.write_text(json.dumps(ids, sort_keys=True, indent=2) + "\n")

    result = _cli("rebuild", "--all", root=src)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Rebuilt 4 tasks")
    rebuilt = json.loads(ids_path.read_text())
    assert rebuilt["next_seqs"]["INT"] == 31
    assert "INT-30" not in rebuilt["map"]
    doctor = _ok("doctor", root=src)
    assert not [f for f in doctor["findings"] if "next_seqs" in f["message"]]
    assert _ok("create", "Five", "--actor", "human:t", root=src)["short_id"] == "INT-31"


def _files(board: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(board)): p.read_bytes()
        for p in board.rglob("*")
        if p.is_file() and "locks" not in p.parts
    }


def test_import_repair_touches_only_task_derived_files(tmp_path: Path) -> None:
    src, (one, two, _three, four) = _board(tmp_path)
    board = src / ".lattice"
    _ok("resource", "create", "db", "--actor", "human:t", root=src)
    _ok("status", four, "done", "--force", "--reason", "fixture", "--actor", "human:t", root=src)
    _ok("archive", four, "--actor", "human:t", root=src)
    # A plan in the wrong place for an active task, a stale snapshot, a snapshot left at
    # the archived location, and a resource snapshot in a non-canonical format.
    (board / "archive" / "plans").mkdir(parents=True, exist_ok=True)
    (board / "plans" / f"{one}.md").rename(board / "archive" / "plans" / f"{one}.md")
    (board / "tasks" / f"{two}.json").write_text("{}\n")
    (board / "tasks" / f"{four}.json").write_text("{}\n")
    resource = next((board / "resources").glob("*/resource.json"))
    resource.write_text(json.dumps(json.loads(resource.read_text())) + "\n")
    before = _files(board)

    repair_task_derived_files(board, reconcile_placement=False)

    after = _files(board)
    derived = {"ids.json", "events/_lifecycle.jsonl"}
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    assert changed == {f"tasks/{two}.json", f"tasks/{four}.json"}
    assert all(p in derived or p.startswith(("tasks/", "archive/tasks/")) for p in changed)
    assert (board / "archive" / "plans" / f"{one}.md").exists()
    assert json.loads((board / "archive" / "tasks" / f"{four}.json").read_text())["id"] == four

    # rebuild --all still reconciles placement and rebuilds resource snapshots.
    assert _cli("rebuild", "--all", root=src).exit_code == 0
    assert (board / "plans" / f"{one}.md").exists()
    assert resource.read_bytes() != after[str(resource.relative_to(board))]


def _break_config(board: Path, case: str) -> None:
    config = board / "config.json"
    if case == "absent":
        config.unlink()
    else:
        config.write_text(
            {"malformed": "{broken\n", "not-an-object": "[]\n", "json-string": '"text"\n'}[case]
        )


CONFIG_CASES = {
    "malformed": ("json_parse", "Invalid JSON in config.json"),
    "absent": ("config", "config.json is missing; this board has no configuration"),
    "not-an-object": ("config", "config.json must hold a JSON object, not list"),
    "json-string": ("config", "config.json must hold a JSON object, not str"),
}


@pytest.mark.parametrize("mode", ["plain", "json"])
@pytest.mark.parametrize("case", CONFIG_CASES)
def test_doctor_reports_a_broken_config_instead_of_crashing(
    tmp_path: Path, case: str, mode: str
) -> None:
    """The scan reads config.json once: a missing, malformed, or non-object config is
    one error finding (exit 1), and the short-ID checks run without a project code.
    Before the scan moved, all three crashed later with a traceback (exit 1, no report)."""
    src, _ids = _board(tmp_path)
    _break_config(src / ".lattice", case)
    check, message = CONFIG_CASES[case]
    report = check_board(src / ".lattice")
    assert [(f["check"], f["level"]) for f in report.findings] == [(check, "error")]
    assert report.findings[0]["message"].startswith(message)
    args = ["doctor"] + (["--json"] if mode == "json" else [])
    result = CliRunner().invoke(cli, args, env={"LATTICE_ROOT": str(src)})
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    if mode == "json":
        payload = json.loads(result.output)
        assert payload["data"]["summary"]["errors"] == 1
        assert payload["data"]["findings"][0]["check"] == check
    else:
        assert f"⚠ {message}" in result.output
        assert "All JSON files valid" not in result.output
        assert result.output.rstrip().endswith("1 error found.")
