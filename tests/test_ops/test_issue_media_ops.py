"""``issue.file`` / ``issue.attach`` / ``issue.detach`` with media (LAT-366), through
``LocalBoard.execute``: atomic filing, limits, validation, dedupe, removal order."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError
from lattice.ops.task_attach import encode_payload
from tests.issue_media_helpers import HTML_AS_PNG, heic, jpeg, mp4, png


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    path = initialized_root / ".lattice" / "config.json"
    config = json.loads(path.read_text())
    config.update(project_code="LAT", issues={"enabled": True})
    path.write_text(json.dumps(config))
    return resolve_board(initialized_root)


def _set_limits(board: LocalBoard, **limits: int) -> None:
    path = board.lattice_dir / "config.json"
    config = json.loads(path.read_text())
    config["issues"].update(limits)
    path.write_text(json.dumps(config))


def run(board: LocalBoard, op: str, **params):  # noqa: ANN003, ANN201
    return board.execute(op, params, Caller(actor="agent:qa"))


def item(content: bytes, name: str = "shot.png", **extra: object) -> dict:
    return {"payload": encode_payload(name, content), **extra}


def frame(t_ms: int, data: bytes | None = None) -> dict:
    return {"t_ms": t_ms, "payload": encode_payload("f.jpg", data or jpeg())}


def refused(board: LocalBoard, op: str, **params) -> OpError:  # noqa: ANN003
    with pytest.raises(OpError) as exc:
        run(board, op, **params)
    return exc.value


def media_files(board: LocalBoard) -> list[str]:
    root = board.lattice_dir / "issues" / "media"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


def test_file_with_media_is_one_atomic_write(board: LocalBoard) -> None:
    video = item(
        mp4(b"one"),
        "repro.mov",
        video={"width": 720, "height": 1280, "duration_ms": 3000},
        frames=[frame(2900), frame(0)],
    )
    result = run(board, "issue.file", text="t", media=(item(png(4, 3)), video, item(png(4, 3))))
    assert [e["type"] for e in result.events] == [
        "issue_filed",
        "issue_media_added",
        "issue_media_added",
    ]
    photo, clip = result.value["media"]
    assert (photo["n"], photo["kind"], photo["width"], photo["height"]) == (1, "photo", 4, 3)
    assert photo["sha256"] == hashlib.sha256(png(4, 3)).hexdigest()
    assert Path(photo["path"]).read_bytes() == png(4, 3)
    assert (clip["n"], clip["content_type"], clip["height"]) == (2, "video/mp4", 1280)
    assert [f["t_ms"] for f in clip["frames"]] == [0, 2900]
    assert Path(clip["frames"][1]["path"]).name == "t0002.900s.jpg"
    log = board.lattice_dir / "issues" / "events" / f"{result.value['id']}.jsonl"
    assert len(log.read_text().splitlines()) == 3


@pytest.mark.parametrize(
    ("media", "reason"),
    [
        ((item(HTML_AS_PNG),), "NOT_MEDIA"),
        ((item(heic(), "x.heic"),), "NOT_MEDIA"),
        (({"payload": {**encode_payload("a.png", png()), "sha256": "0" * 64}},), None),
        ((item(png(), frames=[frame(0)]),), "WRONG_TYPE"),
        ((item(mp4(), frames=[frame(0, png())]),), "WRONG_TYPE"),
        ((item(mp4(), frames=[frame(i) for i in range(9)]),), "WRONG_TYPE"),
        ((item(mp4(), frames=[frame(1), frame(1)]),), "WRONG_TYPE"),
        ((item(mp4(), video={"width": -1}),), "WRONG_TYPE"),
        ((item(mp4(), extra=1),), "WRONG_TYPE"),
        ((item(mp4(), converted_from={"content_type": "video/quicktime"}),), "WRONG_TYPE"),
    ],
)
def test_bad_media_is_refused_before_the_number_is_allocated(
    board: LocalBoard, media: tuple, reason: str | None
) -> None:
    exc = refused(board, "issue.file", text="t", media=media)
    assert exc.code == "VALIDATION_ERROR"
    if reason:
        assert exc.details.get("reason") == reason
    assert not (board.lattice_dir / "issues").exists()


def test_limits_refuse_the_file_and_the_issue(board: LocalBoard) -> None:
    _set_limits(board, max_media_mb=1, max_issue_media_mb=2)
    big = png() + b"\x00" * (1024 * 1024)
    exc = refused(board, "issue.file", text="t", media=(item(big, "big.png"),))
    assert exc.code == "PAYLOAD_TOO_LARGE"
    assert exc.details["reason"] == "MEDIA_FILE_TOO_LARGE"
    assert "big.png is 1.0 MB; the limit is 1 MB per file" in exc.message
    assert "Nothing was filed." in exc.message
    assert not (board.lattice_dir / "issues").exists()

    three = [png() + bytes([i]) * 800_000 for i in range(3)]
    issue = run(board, "issue.file", text="t", media=(item(three[0]), item(three[1]))).value
    before = media_files(board)
    exc = refused(board, "issue.attach", issue=issue["short_id"], media=(item(three[2]),))
    assert (exc.code, exc.details["reason"]) == ("PAYLOAD_TOO_LARGE", "ISSUE_MEDIA_TOO_LARGE")
    assert exc.message.startswith("LAT-I1 would hold 2.3 MB of media; the limit is 2 MB per issue")
    assert media_files(board) == before


def test_attach_dedupes_and_skips_duplicates(board: LocalBoard) -> None:
    issue = run(board, "issue.file", text="t", media=(item(png(1, 1)),)).value["short_id"]
    run(board, "issue.dismiss", issue=issue, reason="closed issues take evidence too")
    same = run(board, "issue.attach", issue=issue, media=(item(png(1, 1), "again.png"),))
    assert same.idempotent and same.events == []
    source = {"content_type": "image/heic", "size_bytes": 9, "sha256": "a" * 64}
    mixed = run(
        board,
        "issue.attach",
        issue=issue,
        media=(item(png(1, 1)), item(jpeg(), "x.heic", converted_from=source)),
    )
    assert [e["data"]["n"] for e in mixed.events] == [2]
    assert mixed.value["media"][1]["converted_from"] == source
    # the same source again: its transcoded bytes may differ, its hash does not
    again = run(
        board, "issue.attach", issue=issue, media=(item(jpeg(1, 1), converted_from=source),)
    )
    assert again.idempotent


def test_detach_deletes_bytes_before_the_event(
    board: LocalBoard, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = item(mp4(), "r.mov", frames=[frame(0), frame(500)])
    issue = run(board, "issue.file", text="t", media=(item(png()), video)).value
    name = issue["short_id"]
    assert refused(board, "issue.detach", issue=name, media="2").code == "VALIDATION_ERROR"

    import lattice.ops.issue_detach as detach

    def crash(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("crash before the event")

    monkeypatch.setattr(detach, "write_issue_events", crash)
    with pytest.raises(RuntimeError):
        run(board, "issue.detach", issue=name, media="2", reason="shows a key")
    assert all(not f.startswith(issue["media"][1]["id"]) for f in media_files(board))
    monkeypatch.undo()

    result = run(board, "issue.detach", issue=name, media="2", reason="shows a key")
    removed = result.value["media"][1]
    assert removed["removed"]["reason"] == "shows a key"
    assert removed["path"] is None and "original_name" not in removed
    assert result.events[0]["provenance"]["reason"] == "shows a key"
    assert len(media_files(board)) == 2  # the issue dir and the photo

    # already removed: idempotent, and bytes a merge restored are deleted again
    restored = Path(issue["media"][1]["path"])
    restored.write_bytes(b"restored")
    again = run(
        board, "issue.detach", issue=name, media=issue["media"][1]["id"].lower(), reason="r"
    )
    assert again.idempotent and not restored.exists()
    assert refused(board, "issue.detach", issue=name, media="9", reason="r").code == "NOT_FOUND"


def test_detach_never_follows_a_planted_symlink(board: LocalBoard, tmp_path: Path) -> None:
    issue = run(board, "issue.file", text="t", media=(item(png()),)).value
    path = Path(issue["media"][0]["path"])
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep me")
    path.unlink()
    path.symlink_to(outside)
    run(board, "issue.detach", issue=issue["short_id"], media="1", reason="r")
    assert not path.exists() and not path.is_symlink()
    assert outside.read_bytes() == b"keep me"
    assert media_files(board) == []


def test_detach_by_an_ordinal_two_clones_both_used_is_a_conflict(board: LocalBoard) -> None:
    """m4: two clones each added media 1; the merged log has both."""
    from lattice.core.events import create_issue_event, serialize_event

    issue = run(board, "issue.file", text="t", media=(item(png()),)).value
    log = board.lattice_dir / "issues" / "events" / f"{issue['id']}.jsonl"
    twin = create_issue_event(
        "issue_media_added",
        issue["id"],
        "agent:other",
        {
            "media_id": "med_01K00000000000000000000009",
            "n": 1,
            "kind": "photo",
            "content_type": "image/png",
            "original_name": "b.png",
            "size_bytes": 1,
            "sha256": "b" * 64,
        },
    )
    with log.open("a") as fh:
        fh.write(serialize_event(twin))
    exc = refused(board, "issue.detach", issue=issue["short_id"], media="1", reason="r")
    assert exc.code == "CONFLICT"
    assert "med_01K00000000000000000000009" in exc.message
