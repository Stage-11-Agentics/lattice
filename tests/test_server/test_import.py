"""``lattice server project import`` (SPEC §11): AC-17 (import), AC-34, AC-35.

One rich source board is built per module and copied per test, so every test
can corrupt or race its own copy.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.errors import OpError
from lattice.server import admin, importer
from lattice.server.journal import Journal
from lattice.server.testing import running_server
from lattice.storage.board_init import create_board
from lattice.storage.integrity import check_board
from lattice.storage.operations import AuthoritativeLogError
from lattice.storage.ownership import board_state
from lattice.templates import load_review_template
from tests.test_server.conftest import board_hash, mint

OVERRIDE = "# Our own code-review prompt\n\nReview {task_id} the house way.\n"
DERIVED_DIRS = ("tasks/", "archive/tasks/")
DERIVED_FILES = ("ids.json", "events/_lifecycle.jsonl")


def _cli(*args: str, root: Path | None = None):
    env = {"LATTICE_ROOT": str(root)} if root is not None else None
    return CliRunner().invoke(cli, list(args), env=env, catch_exceptions=False)


def _ok(*args: str, root: Path) -> dict:
    result = _cli(*args, "--json", root=root)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _build_source(base: Path) -> Path:
    """A local board with every kind of durable data, plus unmanaged and runtime paths."""
    src = base / "checkout"
    src.mkdir()
    result = _cli(
        "init",
        "--path",
        str(src),
        "--project-code",
        "IMP",
        "--actor",
        "human:t",
        "--preset",
        "stage11",
        "--review-mode",
        "triple",
        "--plan-review-mode",
        "inline",
        "--plan-approval",
        "human",
        "--no-setup-claude",
        "--no-setup-agents",
    )
    assert result.exit_code == 0, result.output
    board = src / ".lattice"
    config = json.loads((board / "config.json").read_text())
    config["auto_code_review_on_transition"] = False
    config["auto_plan_review_on_transition"] = False
    (board / "config.json").write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    a = _ok("create", "Alpha", "--actor", "human:t", root=src)["short_id"]
    b = _ok("create", "Beta", "--actor", "human:t", root=src)["short_id"]
    c = _ok("create", "Gamma", "--actor", "human:t", root=src)["short_id"]
    text = base / "text.md"
    text.write_text("# Plan\n\nDo the thing.\n")
    _ok("plan", "write", a, "--file", str(text), "--actor", "human:t", root=src)
    _ok("notes", "write", a, "--file", str(text), "--actor", "human:t", root=src)
    payload = base / "payload.txt"
    payload.write_bytes(b"artifact bytes\x00\x01\n")
    _ok("attach", a, str(payload), "--actor", "human:t", root=src)
    _ok("comment", b, "a comment", "--actor", "human:t", root=src)
    _ok("link", a, "blocks", b, "--actor", "human:t", root=src)
    # A historical short ID above every effective one, absent from ids.json's map.
    _ok(
        "event",
        b,
        "x_legacy_import",
        "--data",
        '{"short_id": "IMP-40"}',
        "--actor",
        "human:t",
        root=src,
    )
    _ok("status", c, "done", "--force", "--reason", "fixture", "--actor", "human:t", root=src)
    _ok("archive", c, "--actor", "human:t", root=src)
    _ok("resource", "create", "db", "--actor", "human:t", root=src)
    _ok("session", "start", "--name", "Argus", "--model", "human", root=src)
    pack = base / "pack.md"
    pack.write_text("review pack\n")
    _ok("board", "write", "plans/review-pack.md", "--file", str(pack), root=src)
    _ok("board", "write", "orchestration/run-state.md", "--file", str(pack), root=src)

    (board / "templates").mkdir(exist_ok=True)
    (board / "templates" / "code-review.md").write_text(OVERRIDE)
    (board / "archive" / "notes" / "scratch").mkdir(parents=True, exist_ok=True)
    (board / "archive" / "notes" / "scratch" / "x.md").write_text("loose\n")
    (board / "reviews").mkdir()
    (board / "reviews" / "r1.md").write_text("an old review\n")
    (board / "exports" / "sub").mkdir(parents=True)
    (board / "exports" / "sub" / "x.txt").write_text("export\n")
    (board / "runner.log").write_text("log\n")
    (board / "events" / ".tmp.abc").write_text("half-written\n")
    (board / "locks").mkdir(exist_ok=True)
    # A counter behind the logs: a doctor warning that import's repair fixes.
    ids = json.loads((board / "ids.json").read_text())
    ids["next_seqs"]["IMP"] = 2
    (board / "ids.json").write_text(json.dumps(ids, sort_keys=True, indent=2) + "\n")
    return src


@pytest.fixture(scope="module")
def pristine(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build_source(tmp_path_factory.mktemp("import-source"))


@pytest.fixture()
def source(pristine: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "checkout"
    shutil.copytree(pristine, copy, symlinks=True)
    return copy


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    root = tmp_path / "server-root"
    admin.init_root(root)
    return root


def _import(root: Path, source: Path, slug: str = "imp", *, as_json: bool = True):
    args = ["server", "project", "import", slug, "--from", str(source), "--root", str(root)]
    return _cli(*args, *(["--json"] if as_json else []))


def _tree(base: Path) -> dict[str, tuple]:
    """Every path under *base* (links not followed): kind, bytes, and mtime."""
    out: dict[str, tuple] = {}
    for dirpath, dirs, files in os.walk(base):
        for name in dirs + files:
            path = Path(dirpath) / name
            st = path.lstat()
            data = path.read_bytes() if stat.S_ISREG(st.st_mode) else None
            out[str(path.relative_to(base))] = (stat.S_IFMT(st.st_mode), data, st.st_mtime_ns)
    return out


def _durable_files(board: Path) -> dict[str, bytes]:
    from lattice.storage.ownership import PathClass, classify_path

    out = {}
    for dirpath, _dirs, files in os.walk(board):
        for name in files:
            path = Path(dirpath) / name
            rel = path.relative_to(board)
            if classify_path(rel) in (PathClass.DURABLE, PathClass.WORKSPACE):
                out[rel.as_posix()] = path.read_bytes()
    return out


def _is_derived(path: str) -> bool:
    return path in DERIVED_FILES or path.startswith(DERIVED_DIRS)


def _assert_nothing_created(root: Path, slug: str = "imp") -> None:
    assert not (root / "projects" / slug).exists()
    assert sorted(p.name for p in (root / "projects").iterdir()) == []


# ---------------------------------------------------------------------------
# AC-35: everything moves, byte for byte
# ---------------------------------------------------------------------------


def test_import_copies_every_durable_file_byte_for_byte(root: Path, source: Path) -> None:
    before = _tree(source)
    result = _import(root, source)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    board = root / "projects" / "imp" / ".lattice"

    src_files = _durable_files(source / ".lattice")
    dst_files = _durable_files(board)
    for path, content in src_files.items():
        assert path in dst_files, f"{path} was not imported"
        if not _is_derived(path):
            assert dst_files[path] == content, f"{path} differs"
    assert {p for p in dst_files if p not in src_files and not _is_derived(p)} == set()
    assert data["copied"] == len(src_files)

    # The review configuration and the prompt override still apply.
    assert (board / "config.json").read_bytes() == (source / ".lattice/config.json").read_bytes()
    config = json.loads((board / "config.json").read_text())
    assert (config["review_mode"], config["plan_review_mode"], config["plan_approval"]) == (
        "triple",
        "inline",
        "human",
    )
    assert config["auto_code_review_on_transition"] is False
    assert load_review_template(board, "code-review") == OVERRIDE

    # Every path not moved is named, with its class, and is absent on the server.
    not_copied = {(row["path"], row["class"]) for row in data["not_copied"]}
    for row in (
        ("reviews/", "unmanaged"),
        ("reviews/r1.md", "unmanaged"),
        ("exports/", "unmanaged"),
        ("exports/sub/", "unmanaged"),
        ("exports/sub/x.txt", "unmanaged"),
        ("runner.log", "unmanaged"),
        ("locks/", "runtime"),
        ("events/.tmp.abc", "temporary"),
    ):
        assert row in not_copied
    assert [row["path"] for row in data["not_copied"]] == sorted(
        row["path"] for row in data["not_copied"]
    )
    for path in ("reviews", "exports", "runner.log", "events/.tmp.abc"):
        assert not (board / path).exists()
    assert data["non_canonical"] == ["archive/notes/scratch/x.md", "plans/review-pack.md"]
    assert (board / "plans" / "review-pack.md").read_text() == "review pack\n"

    # A clean board with a new epoch at head 0, its baseline covering every log.
    assert not check_board(board).errors
    doctor = _cli("doctor", "--json", root=board.parent)
    assert json.loads(doctor.output)["data"]["summary"]["errors"] == 0, doctor.output
    journal = Journal.load(board)
    assert journal.head_seq == 0 and journal.epoch == data["epoch"]
    logs = {p for p in dst_files if p.endswith(".jsonl")}
    assert logs <= set(journal.baseline)
    assert board_state(board) == "hosted"
    assert data["move_steps"][3]["commands"] == ["lattice remote attach <alias> imp"]

    # The short-ID repair raised the lagging counter above the log floor (IMP-40).
    ids = json.loads((board / "ids.json").read_text())
    assert "IMP-40" not in ids["map"]
    assert ids["next_seqs"]["IMP"] == 41
    assert any("next_seqs" in f["message"] for f in data["doctor"]["findings"])

    assert _tree(source) == before
    assert not list((root / "projects").glob(".importing-*"))


def test_import_plain_output_names_both_lists_and_the_move(root: Path, source: Path) -> None:
    result = _import(root, source, as_json=False)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Imported imp from" in out and "doctor passed with" in out
    assert "  reviews/r1.md  (unmanaged)" in out
    assert "  plans/review-pack.md" in out
    assert "lattice remote attach <alias> imp" in out
    assert "git rm -r --cached -q --ignore-unmatch .lattice" in out


def test_imported_project_serves_and_allocates_above_the_floor(root: Path, source: Path) -> None:
    assert _import(root, source).exit_code == 0
    token = mint(root)
    with running_server(root) as server:
        status, _, body = server.request("GET", "/v1/projects/imp/tasks", token=token)
        assert status == 200, body
        status, _, body = server.op("imp", "task.create", {"title": "new"}, token=token)
        assert status == 200, body
        assert body["data"]["result"]["task"]["short_id"] == "IMP-41"


def test_a_board_without_a_project_code_says_how_to_add_one(root: Path, tmp_path: Path) -> None:
    src = tmp_path / "plain"
    src.mkdir()
    create_board(src)
    result = _import(root, src, as_json=False)
    assert result.exit_code == 0, result.output
    assert "lattice set-project-code" in result.output
    board = root / "projects" / "imp" / ".lattice"
    assert (board / "config.json").read_bytes() == (src / ".lattice" / "config.json").read_bytes()


# ---------------------------------------------------------------------------
# AC-17: refusals create nothing and leave the source untouched
# ---------------------------------------------------------------------------


def _corrupt_log(board: Path) -> str:
    log = sorted((board / "events").glob("task_*.jsonl"))[0]
    lines = log.read_text().splitlines(keepends=True)
    lines.insert(1, "{not json\n")
    log.write_text("".join(lines))
    return log.name


def _symlink_event(board: Path) -> str:
    target = board.parent / "elsewhere.jsonl"
    target.write_text("{}\n")
    os.symlink(target, board / "events" / "task_linked.jsonl")
    return "events/task_linked.jsonl"


def _symlink_plans_dir(board: Path) -> str:
    outside = board.parent / "outside-plans"
    outside.mkdir()
    (outside / "p.md").write_text("x\n")
    os.symlink(outside, board / "plans" / "shared")
    return "plans/shared"


def _symlink_orchestration(board: Path) -> str:
    os.symlink("/etc/hosts", board / "orchestration" / "hosts")
    return "orchestration/hosts"


def _fifo_in_notes(board: Path) -> str:
    os.mkfifo(board / "notes" / "pipe")
    return "notes/pipe"


def _symlinked_board(board: Path) -> str:
    real = board.parent / "real-board"
    board.rename(real)
    os.symlink(real, board)
    return "."


UNSAFE = {
    "symlinked-event-log": _symlink_event,
    "symlinked-plans-dir": _symlink_plans_dir,
    "symlink-in-orchestration": _symlink_orchestration,
    "fifo-in-notes": _fifo_in_notes,
    "symlinked-board": _symlinked_board,
}


@pytest.mark.parametrize("as_json", [True, False], ids=["json", "plain"])
@pytest.mark.parametrize("make", UNSAFE.values(), ids=UNSAFE.keys())
def test_unsafe_paths_are_refused_naming_the_path(
    root: Path, source: Path, make: Callable[[Path], str], as_json: bool
) -> None:
    path = make(source / ".lattice")
    before = _tree(source)
    result = _import(root, source, as_json=as_json)
    assert result.exit_code == 1
    if as_json:
        error = json.loads(result.output)["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["path"] == path
        assert f".lattice/{path}" in error["message"]
    else:
        assert f".lattice/{path}" in result.output
    _assert_nothing_created(root)
    assert _tree(source) == before


@pytest.mark.parametrize("as_json", [True, False], ids=["json", "plain"])
def test_a_board_that_fails_doctor_is_refused_with_its_findings(
    root: Path, source: Path, as_json: bool
) -> None:
    name = _corrupt_log(source / ".lattice")
    before = _tree(source)
    result = _import(root, source, as_json=as_json)
    assert result.exit_code == 1
    if as_json:
        error = json.loads(result.output)["error"]
        assert error["code"] == "INTEGRITY_ERROR"
        assert error["details"]["summary"]["errors"] >= 1
        assert any(name in f["message"] for f in error["details"]["findings"])
    else:
        assert "fails lattice doctor" in result.output
        assert f"Invalid JSON at line 2 in {name}" in result.output
        assert f"{source / '.lattice' / 'events' / name}:" in result.output
        assert ".importing-" not in result.output
    _assert_nothing_created(root)
    assert _tree(source) == before


@pytest.mark.parametrize("as_json", [True, False], ids=["json", "plain"])
def test_an_existing_slug_is_refused_and_left_alone(
    root: Path, source: Path, as_json: bool
) -> None:
    admin.create_project(root, "imp", code="OLD")
    before = board_hash(root, "imp")
    result = _import(root, source, as_json=as_json)
    assert result.exit_code == 1
    if as_json:
        assert json.loads(result.output)["error"]["code"] == "CONFLICT"
    assert "already exists" in result.output
    assert board_hash(root, "imp") == before
    assert not list((root / "projects").glob(".importing-*"))


def test_arguments_are_checked_in_order_before_anything_is_read(
    root: Path, source: Path, tmp_path: Path
) -> None:
    def code(*args: str) -> str:
        result = _cli("server", "project", "import", *args, "--json")
        assert result.exit_code == 1, result.output
        return json.loads(result.output)["error"]["code"]

    missing = str(tmp_path / "missing")
    assert code("Bad_Slug", "--from", missing, "--root", str(root)) == "VALIDATION_ERROR"
    assert code("imp", "--from", missing, "--root", str(tmp_path / "nope")) == "NOT_INITIALIZED"
    admin.create_project(root, "taken")
    assert code("taken", "--from", missing, "--root", str(root)) == "CONFLICT"
    assert code("imp", "--from", missing, "--root", str(root)) == "NOT_FOUND"
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    assert code("imp", "--from", str(a_file), "--root", str(root)) == "VALIDATION_ERROR"
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _cli("server", "project", "import", "imp", "--from", str(empty), "--root", str(root))
    assert result.exit_code == 1 and "contains no .lattice/ board" in result.output
    assert not (root / "projects" / "imp").exists()


# ---------------------------------------------------------------------------
# A source that changes during the copy, and failures at every later step
# ---------------------------------------------------------------------------


def _hook_first_read(monkeypatch: pytest.MonkeyPatch, action: Callable[[], None]) -> None:
    original = importer._read_file
    fired = []

    def read(lattice_fd: int, path: str, scan: object) -> bytes:
        if not fired:
            fired.append(path)
            action()
        return original(lattice_fd, path, scan)

    monkeypatch.setattr(importer, "_read_file", read)


def test_a_file_changed_between_walk_and_copy_is_refused(
    root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = source / ".lattice" / "config.json"
    _hook_first_read(monkeypatch, lambda: config.write_text(config.read_text() + " "))
    result = _import(root, source)
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error["code"] == "CONFLICT" and error["details"]["reason"] == "SOURCE_CHANGED"
    assert error["details"]["path"] == "config.json"
    _assert_nothing_created(root)


def test_a_directory_swapped_for_a_symlink_is_never_followed(
    root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = source / ".lattice"
    decoy = source / "decoy-plans"

    def swap() -> None:
        shutil.copytree(board / "plans", decoy)
        (decoy / "injected.md").write_text("not from the board\n")
        shutil.rmtree(board / "plans")
        os.symlink(decoy, board / "plans")

    _hook_first_read(monkeypatch, swap)
    result = _import(root, source)
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error["code"] == "CONFLICT" and error["details"]["path"].startswith("plans")
    _assert_nothing_created(root)


def test_a_file_added_during_the_copy_is_refused(
    root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    late = source / ".lattice" / "notes" / "late.md"
    _hook_first_read(monkeypatch, lambda: late.write_text("late\n"))
    result = _import(root, source)
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error["code"] == "CONFLICT" and error["details"]["path"] == "notes/late.md"
    _assert_nothing_created(root)


def _fail(exc: BaseException) -> Callable[..., None]:
    def raise_it(*_args: object, **_kwargs: object) -> None:
        raise exc

    return raise_it


@pytest.mark.parametrize(
    ("target", "exc", "code"),
    [
        ("atomic_write", OSError(28, "No space left on device"), None),
        ("check_board", RuntimeError("doctor crashed"), None),
        (
            "repair_task_derived_files",
            AuthoritativeLogError("conflicting lifecycle event"),
            "INTEGRITY_ERROR",
        ),
        ("seal_new_board", OSError(5, "I/O error"), None),
    ],
    ids=["copy", "doctor", "repair", "seal"],
)
def test_a_failure_at_any_step_removes_the_staging_board(
    root: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    exc: BaseException,
    code: str | None,
) -> None:
    monkeypatch.setattr(importer, target, _fail(exc))
    if code is None:
        with pytest.raises(type(exc)):
            importer.import_project(root, "imp", source)
    else:
        with pytest.raises(OpError) as raised:
            importer.import_project(root, "imp", source)
        assert raised.value.code == code
    _assert_nothing_created(root)


def test_a_slug_taken_during_the_import_is_refused_at_publication(
    root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = importer.check_board

    def racing_check(board: Path):  # noqa: ANN202 - DoctorReport
        admin.create_project(root, "imp", code="WON")
        return original(board)

    monkeypatch.setattr(importer, "check_board", racing_check)
    with pytest.raises(OpError) as raised:
        importer.import_project(root, "imp", source)
    assert raised.value.code == "CONFLICT"
    config = json.loads((root / "projects" / "imp" / ".lattice" / "config.json").read_text())
    assert config["project_code"] == "WON"
    assert not list((root / "projects").glob(".importing-*"))
