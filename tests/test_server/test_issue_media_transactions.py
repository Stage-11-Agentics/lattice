"""Hosted issue media through the real server's transactions (SPEC §8.4, §8.12):
commit then finalize, rollback before the commit point, detach ordering, crash
reconciliation on reload, retried operations, and the places media bytes must
never appear (journal, receipts, stream, sync, audit history).
"""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.core.errors import OpError
from lattice.core.ids import generate_issue_id, generate_media_id, generate_op_id
from lattice.core.issue_media import frame_name
from lattice.ops import Caller
from lattice.ops.task_attach import encode_payload
from lattice.server import admin, syncstate
from lattice.server.issue_media import HostedIssueMedia
from lattice.server.testing import ServerHandle, make_root, open_stream, running_server, wait_for
from lattice.storage.issues import read_issue_snapshot
from tests.issue_media_helpers import jpeg, mp4
from tests.test_server.audit_helpers import FAST, head_tree, last_committed_seq
from tests.test_server.conftest import board_hash, mint
from tests.test_server.faults import Injector, install
from tests.test_server.test_issue_media_routes import (  # noqa: F401 - the fixtures
    SLUG,
    blob,
    board_of,
    call,
    error_code,
    file_issue,
    filed,
    item,
    manifest_dir,
    media_server,
    names,
    payload,
    sha,
    stage_dir,
    stage_ok,
)


@pytest.fixture()
def root(root: Path) -> Path:
    """The conftest root (two projects, audit off) with the issue log on in both."""
    for slug in (SLUG, "beta"):
        admin.set_project_config(root, slug, {"issues.enabled": True})
    return root


@pytest.fixture()
def token(root: Path) -> str:
    return mint(root, projects=[SLUG])


def board_journal(board: Path) -> list[dict]:
    path = board / "hosted" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def journal(root: Path) -> list[dict]:
    return board_journal(board_of(root))


def media_files(root: Path) -> list[str]:
    base = board_of(root) / "issues" / "media"
    return sorted(str(p.relative_to(base)) for p in base.rglob("*")) if base.exists() else []


def stored(root: Path, issue: dict, index: int = 0, ext: str = ".png") -> Path:
    return (
        board_of(root) / "issues" / "media" / issue["id"] / f"{issue['media'][index]['id']}{ext}"
    )


class Untouched:
    """What a refused operation must leave alone: the board's files, the journal, the
    media tree, and the manifest directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.boards = board_hash(root, SLUG)
        self.journal = journal(root)
        self.media = media_files(root)

    def check(self) -> None:
        assert board_hash(self.root, SLUG) == self.boards
        assert journal(self.root) == self.journal
        assert media_files(self.root) == self.media
        assert names(manifest_dir(self.root)) == []


# ---------------------------------------------------------------------------
# issue.file: commit, then finalize
# ---------------------------------------------------------------------------


def test_file_commits_issue_events_and_snapshot_before_bytes_are_published(
    server: ServerHandle, root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = blob(700, b"order")
    stage_ok(server, token, data)
    seen: dict = {}
    real = HostedIssueMedia.finalize_operation

    def spy(self: HostedIssueMedia, op_id: str) -> bool:
        manifest = json.loads(self._manifest_path(op_id).read_text())
        issue_id = manifest["issue_id"]
        target = self.board / manifest["objects"][0]["target"]
        log = self.board / "issues" / "events" / f"{issue_id}.jsonl"
        seen.update(
            op_id=op_id,
            target_present=target.exists(),
            snapshot=bool((read_issue_snapshot(self.board, issue_id) or {}).get("media")),
            logged="issue_media_added" in log.read_text(),
            journaled=any(line.get("op_id") == op_id for line in board_journal(self.board)),
        )
        return real(self, op_id)

    monkeypatch.setattr(HostedIssueMedia, "finalize_operation", spy)
    op_id = generate_op_id()
    status, _, body = file_issue(server, token, [item(data)], op_id=op_id)
    assert status == 200, body
    view = body["data"]["result"]["value"]

    assert seen == {
        "op_id": op_id,
        "target_present": False,  # the bytes were not there when the commit landed
        "snapshot": True,
        "logged": True,
        "journaled": True,
    }
    assert stored(root, view).read_bytes() == data
    assert view["media"][0]["sha256"] == sha(data)
    assert names(manifest_dir(root)) == [] and names(stage_dir(root)) == []  # consumed
    assert [e["type"] for e in body["data"]["result"]["events"]] == [
        "issue_filed",
        "issue_media_added",
    ]


# ---------------------------------------------------------------------------
# Failures before the commit point leave nothing
# ---------------------------------------------------------------------------


def test_a_staged_object_that_does_not_exist_is_refused_and_nothing_is_written(
    server: ServerHandle, root: Path, token: str
) -> None:
    before = Untouched(root)
    status, _, body = file_issue(server, token, [item(blob(300, b"never uploaded"))])
    assert status == 404, body
    assert body["error"]["code"] == "NOT_FOUND"
    before.check()


def test_a_size_that_does_not_match_the_staged_object_is_refused(
    server: ServerHandle, root: Path, token: str
) -> None:
    data = blob(300, b"size")
    stage_ok(server, token, data)
    before = Untouched(root)
    wrong = {"payload": {**payload(data), "size": 299}}
    status, _, body = file_issue(server, token, [wrong])
    assert status == 400, body
    assert body["error"]["code"] == "VALIDATION_ERROR"
    before.check()
    # The upload is still there: the client may correct the size and go on.
    status, _, body = file_issue(server, token, [item(data)])
    assert status == 200, body


def test_a_staged_object_whose_bytes_changed_is_refused(
    server: ServerHandle, root: Path, token: str
) -> None:
    data = blob(300, b"swap")
    stage_ok(server, token, data)
    (stage_dir(root) / f"{sha(data)}.blob").write_bytes(blob(300, b"other"))
    before = Untouched(root)
    status, _, body = file_issue(server, token, [item(data)])
    assert status == 500 and body["error"]["code"] == "INTEGRITY_ERROR", body
    before.check()


def test_an_issue_over_the_server_per_issue_limit_is_refused_with_nothing_on_disk(
    root: Path, token: str
) -> None:
    with media_server(root, max_issue_media_issue_bytes=300) as server:
        one, two, three = blob(200, b"1"), blob(200, b"2"), blob(100, b"3")
        for data in (one, two, three):
            stage_ok(server, token, data)
        before = Untouched(root)
        status, _, body = file_issue(server, token, [item(one), item(two)])
        assert status == 413 and body["error"]["code"] == "PAYLOAD_TOO_LARGE", body
        before.check()

        # An attach counts what the issue already holds, frames included.
        view = filed(server, token, [item(one)])
        held = Untouched(root)
        status, _, body = server.op(
            SLUG,
            "issue.attach",
            {"issue": view["id"], "media": [item(two)]},
            token=token,
        )
        assert status == 413 and body["error"]["code"] == "PAYLOAD_TOO_LARGE", body
        assert stored(root, view).read_bytes() == one  # what was there is untouched
        assert media_files(root) == held.media and names(manifest_dir(root)) == []
        status, _, _ = server.op(
            SLUG,
            "issue.attach",
            {"issue": view["id"], "media": [item(three)]},
            token=token,
        )
        assert status == 200  # 200 + 100 fits


def test_a_failure_after_the_manifest_but_before_the_commit_rolls_everything_back(
    server: ServerHandle, root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = blob(500, b"rollback")
    stage_ok(server, token, data)
    before = Untouched(root)
    op_id = generate_op_id()
    with monkeypatch.context() as m:
        injector = install(m, Injector("journal.write"))
        status, _, body = file_issue(server, token, [item(data)], op_id=op_id)
    assert injector.fired and status >= 500, body
    before.check()  # board, journal, media tree and manifests exactly as before
    assert names(stage_dir(root)) == []  # the consumed upload is gone with its manifest

    # Nothing committed, so the same op_id is free: re-upload and it applies once.
    status, _, _ = file_issue(server, token, [item(data)], op_id=op_id)
    assert status == 404  # the stage was consumed by the failed attempt
    stage_ok(server, token, data)
    status, _, body = file_issue(server, token, [item(data)], op_id=op_id)
    assert status == 200, body
    assert [e["op_id"] for e in journal(root)].count(op_id) == 1


# ---------------------------------------------------------------------------
# issue.attach and issue.detach
# ---------------------------------------------------------------------------


def test_attach_publishes_originals_and_frames_after_its_commit(
    server: ServerHandle, root: Path, token: str
) -> None:
    clip, frame = mp4(b"attach" + b"." * 300), jpeg(32, 24)
    stage_ok(server, token, clip)
    stage_ok(server, token, frame)
    view = filed(server, token, [])
    status, _, body = server.op(
        SLUG,
        "issue.attach",
        {
            "issue": view["id"],
            "media": [
                item(
                    clip,
                    "clip.mp4",
                    video={"width": 32, "height": 24, "duration_ms": 900},
                    frames=[{"t_ms": 500, "payload": payload(frame, "f.jpg")}],
                )
            ],
        },
        token=token,
    )
    assert status == 200, body
    media = body["data"]["result"]["value"]["media"][0]
    base = board_of(root) / "issues" / "media" / view["id"]
    assert (base / f"{media['id']}.mp4").read_bytes() == clip
    assert (base / f"{media['id']}.frames" / frame_name(500)).read_bytes() == frame
    assert names(manifest_dir(root)) == [] and names(stage_dir(root)) == []


def test_detach_records_its_event_before_the_bytes_go_and_then_removes_them(
    server: ServerHandle, root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip, frame = mp4(b"detach" + b"." * 300), jpeg(32, 24)
    for data in (clip, frame):
        stage_ok(server, token, data)
    view = filed(
        server,
        token,
        [
            item(
                clip,
                "clip.mp4",
                video={"width": 32, "height": 24, "duration_ms": 900},
                frames=[{"t_ms": 500, "payload": payload(frame, "f.jpg")}],
            )
        ],
    )
    media_id = view["media"][0]["id"]
    original = stored(root, view, ext=".mp4")
    frames = original.with_name(f"{media_id}.frames")
    assert original.is_file() and frames.is_dir()

    seen: dict = {}
    real = HostedIssueMedia.finalize_removed

    def spy(self: HostedIssueMedia, events: list) -> None:
        snapshot = read_issue_snapshot(self.board, view["id"]) or {}
        seen.update(
            removed=[bool(m.get("removed")) for m in snapshot["media"]],
            journaled=board_journal(self.board)[-1]["op"],
            bytes_still_there=original.is_file() and frames.is_dir(),
        )
        real(self, events)

    monkeypatch.setattr(HostedIssueMedia, "finalize_removed", spy)
    op_id = generate_op_id()
    params = {"issue": view["id"], "media": media_id, "reason": "wrong build"}
    status, _, body = server.op(SLUG, "issue.detach", params, token=token, op_id=op_id)
    assert status == 200, body

    assert seen == {
        "removed": [True],
        "journaled": "issue.detach",
        "bytes_still_there": True,  # the event committed first
    }
    assert not original.exists() and not frames.exists()  # then the bytes went
    assert "issue_media_removed" in [e["type"] for e in body["data"]["result"]["events"]]
    # A retried detach replays: it neither fails nor writes again.
    before = len(journal(root))
    status, _, again = server.op(SLUG, "issue.detach", params, token=token, op_id=op_id)
    assert status == 200 and len(journal(root)) == before
    assert again["data"]["result"]["replayed"] is True


def test_a_failed_detach_commit_keeps_the_bytes(
    server: ServerHandle, root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = blob(400, b"keep")
    stage_ok(server, token, data)
    view = filed(server, token, [item(data)])
    before = Untouched(root)
    with monkeypatch.context() as m:
        install(m, Injector("journal.write"))
        status, _, _ = server.op(
            SLUG,
            "issue.detach",
            {"issue": view["id"], "media": "1", "reason": "r"},
            token=token,
        )
    assert status >= 500
    before.check()
    assert (
        stored(root, view).read_bytes() == data
    )  # the commit rolled back, so nothing was removed


# ---------------------------------------------------------------------------
# Inline and local payload shapes
# ---------------------------------------------------------------------------


def test_inline_base64_payloads_are_refused_by_the_hosted_server(
    server: ServerHandle, root: Path, token: str
) -> None:
    data, frame = mp4(b"inline" + b"." * 300), jpeg()
    view = filed(server, token, [])
    before = Untouched(root)
    inline = {"payload": encode_payload("shot.mp4", data)}
    status, _, body = file_issue(server, token, [inline])
    assert status == 400 and body["error"]["code"] == "HOSTED_MEDIA_INLINE_UNSUPPORTED", body
    status, _, body = server.op(
        SLUG, "issue.attach", {"issue": view["id"], "media": [inline]}, token=token
    )
    assert status == 400 and body["error"]["code"] == "HOSTED_MEDIA_INLINE_UNSUPPORTED", body
    # A staged original with an inline frame is refused too.
    stage_ok(server, token, data)
    mixed = item(
        data,
        "shot.mp4",
        video={"width": 8, "height": 8, "duration_ms": 10},
        frames=[{"t_ms": 0, "payload": encode_payload("f.jpg", frame)}],
    )
    status, _, body = file_issue(server, token, [mixed])
    assert status == 400 and body["error"]["code"] == "HOSTED_MEDIA_INLINE_UNSUPPORTED", body
    before.check()


@pytest.fixture()
def local_board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    path = initialized_root / ".lattice" / "config.json"
    config = json.loads(path.read_text())
    config.update(project_code="LAT", issues={"enabled": True})
    path.write_text(json.dumps(config))
    return resolve_board(initialized_root)


def test_staged_payloads_are_refused_by_a_local_operation(local_board: LocalBoard) -> None:
    data = blob(300, b"local")
    staged = item(data)
    inline_with_staged_frame = {
        "payload": encode_payload("c.mp4", mp4(b"x")),
        "video": {"width": 8, "height": 8, "duration_ms": 10},
        "frames": [{"t_ms": 0, "payload": payload(jpeg(), "f.jpg")}],
    }
    for media in (staged, inline_with_staged_frame):
        with pytest.raises(OpError) as exc:
            local_board.execute(
                "issue.file", {"title": "t", "media": (media,)}, Caller(actor="agent:qa")
            )
        assert exc.value.code == "MEDIA_STAGE_UNAVAILABLE"
    issue = local_board.execute("issue.file", {"title": "t"}, Caller(actor="agent:qa")).value
    with pytest.raises(OpError) as exc:
        local_board.execute(
            "issue.attach", {"issue": issue["id"], "media": (staged,)}, Caller(actor="agent:qa")
        )
    assert exc.value.code == "MEDIA_STAGE_UNAVAILABLE"
    media_dir = local_board.lattice_dir / "issues" / "media"
    assert not media_dir.exists() or not list(media_dir.rglob("*"))


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_a_retried_operation_id_is_applied_once(
    server: ServerHandle, root: Path, token: str
) -> None:
    data = blob(500, b"retry")
    stage_ok(server, token, data)
    op_id = generate_op_id()
    status, _, first = file_issue(server, token, [item(data)], op_id=op_id)
    assert status == 200, first
    view = first["data"]["result"]["value"]
    assert names(stage_dir(root)) == []  # consumed: a replay must not need it

    status, _, again = file_issue(server, token, [item(data)], op_id=op_id)
    assert status == 200 and again["data"]["result"]["replayed"] is True
    assert again["data"]["result"]["value"]["id"] == view["id"]
    assert [e["op_id"] for e in journal(root)].count(op_id) == 1
    assert media_files(root) == [view["id"], f"{view['id']}/{view['media'][0]['id']}.png"]
    # The same id with other arguments is refused, and writes nothing.
    status, _, body = file_issue(server, token, [item(data)], op_id=op_id, title="other")
    assert status == 409 and body["error"]["details"]["reason"] == "OP_ID_REUSED", body


# ---------------------------------------------------------------------------
# Crash and reload
# ---------------------------------------------------------------------------


def test_a_commit_whose_finalize_never_ran_is_published_when_the_project_reloads(
    root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = blob(800, b"crash")
    with running_server(root) as server:
        stage_ok(server, token, data)
        with monkeypatch.context() as m:  # the process "dies" before finalize
            m.setattr(HostedIssueMedia, "finalize_operation", lambda self, op_id: False)
            view = filed(server, token, [item(data)])
        target = stored(root, view)
        assert not target.exists()  # committed metadata, no bytes yet
        assert len(names(manifest_dir(root))) == 1
        assert f"{sha(data)}.blob" in names(stage_dir(root))
        path = f"/v1/projects/{SLUG}/issues/media/{view['id']}/{view['media'][0]['id']}"
        assert call(server, "GET", path, token=token)[0] == 404  # absent, not corrupt

    with running_server(root) as server:  # a restart reloads the project
        assert target.read_bytes() == data
        assert names(manifest_dir(root)) == [] and names(stage_dir(root)) == []
        status, _, body = call(server, "GET", path, token=token)
        assert (status, body) == (200, data)
        assert server.project(SLUG).issue_media.published_sizes == {sha(data): 800}


def test_reload_aborts_an_uncommitted_manifest_and_sweeps_orphaned_media(
    root: Path, token: str
) -> None:
    kept_data, lost = blob(500, b"kept"), blob(500, b"lost")
    with running_server(root) as server:
        stage_ok(server, token, kept_data)
        stage_ok(server, token, lost)
        kept = filed(server, token, [item(kept_data)])
        op_id, issue_id, media_id = generate_op_id(), generate_issue_id(), generate_media_id()
        server.project(SLUG).issue_media.add_manifest(  # a crash after the manifest
            op_id,
            issue_id,
            [
                {
                    "media_id": media_id,
                    "t_ms": None,
                    "sha256": sha(lost),
                    "size_bytes": 500,
                    "target": f"issues/media/{issue_id}/{media_id}.png",
                }
            ],
        )
        assert len(names(manifest_dir(root))) == 1
    media_root = board_of(root) / "issues" / "media"
    orphan = media_root / generate_issue_id()  # an issue directory no issue owns
    orphan.mkdir()
    (orphan / f"{generate_media_id()}.png").write_bytes(kept_data)
    stray = media_root / kept["id"] / f"{generate_media_id()}.png"  # media the issue lacks
    stray.write_bytes(kept_data)

    with running_server(root):
        assert names(manifest_dir(root)) == []  # never committed: aborted
        assert f"{sha(lost)}.blob" not in names(stage_dir(root))  # and its stage released
        assert not orphan.exists() and not stray.exists()
        assert stored(root, kept).read_bytes() == kept_data  # committed media is kept


# ---------------------------------------------------------------------------
# Media bytes never travel in the wrong places
# ---------------------------------------------------------------------------

MARKER = b"UNIQUE-MEDIA-MARKER-0f3a9c"


def test_media_bytes_never_enter_the_journal_receipts_stream_or_sync(
    server: ServerHandle, root: Path, token: str
) -> None:
    data = blob(900, MARKER)
    assert MARKER in data
    encoded = [base64.b64encode(data[i:]).decode() for i in range(3)]  # any alignment
    stage_ok(server, token, data)
    server.op(SLUG, "task.create", {"title": "t"}, token=token)  # so the head has a hash
    _, _, before = server.request("GET", f"/v1/projects/{SLUG}/sync", token=token)
    head = before["data"]

    reader = open_stream(server.url, SLUG, token)
    try:
        assert reader.next().event == "heartbeat"
        status, _, body = file_issue(server, token, [item(data)])
        assert status == 200, body
        message = reader.next_of("journal")
    finally:
        reader.close()
    view = body["data"]["result"]["value"]
    wire = json.dumps(message.data).encode()
    assert MARKER not in wire and not any(e.encode() in wire for e in encoded)
    assert not any(p.startswith("issues/media/") for p in message.data["paths"])
    assert MARKER not in json.dumps(body).encode()

    # Everything the server keeps on the board, except the media store itself.
    board = board_of(root)
    for path in board.rglob("*"):
        if path.is_file() and "issues/media" not in path.as_posix():
            content = path.read_bytes()
            assert MARKER not in content, path.relative_to(board)
    for path in (board / "hosted").rglob("*"):
        if path.is_file():
            assert not any(e.encode() in path.read_bytes() for e in encoded), path

    # Sync: a full reset, the manifest, and a delta from before the write.
    query = f"since={head['head_seq']}&epoch={head['epoch']}&hash={head['head_hash']}"
    for suffix in ("since=0", "since=0&manifest=1", query):
        status, _, raw = server.request("GET", f"/v1/projects/{SLUG}/sync?{suffix}", token=token)
        assert status == 200
        files = raw["data"]["files"]
        assert not any(p.startswith("issues/media") for p in files), suffix
        assert not any(p.startswith("issues/media") for p in raw["data"]["removed"])
        assert MARKER not in json.dumps(raw).encode(), suffix
        for spec in files.values():
            if "content_b64" in spec:
                assert MARKER not in base64.b64decode(spec["content_b64"])
    assert (
        f"issues/{view['id']}.json"
        in server.request("GET", f"/v1/projects/{SLUG}/sync?{query}", token=token)[2]["data"][
            "files"
        ]
    )
    assert not any(p.startswith("issues/media") for p in syncstate.synced_files(board))
    # The files route is for board files only: it will not hand out media.
    path = f"issues/media/{view['id']}/{view['media'][0]['id']}.png"
    status, _ = server.request("GET", f"/v1/projects/{SLUG}/files/{path}", token=token)[:2]
    assert status == 404


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.timeout(60)
def test_media_bytes_never_reach_the_audit_history(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={SLUG: {"code": "ALP"}}, config=FAST)
    admin.set_project_config(root, SLUG, {"issues.enabled": True})
    token = mint(root, projects=[SLUG])
    data = blob(900, MARKER)
    with running_server(root) as server:
        stage_ok(server, token, data)
        view = filed(server, token, [item(data)])
        directory = root / "projects" / SLUG
        head = server.project(SLUG).journal.head_seq
        assert wait_for(lambda: last_committed_seq(directory) == head, timeout=10)
        tree = head_tree(directory)
        assert f"issues/{view['id']}.json" in tree  # the issue itself is audited
        assert not any(path.startswith("issues/media") for path in tree)
        assert not any(MARKER in content for content in tree.values())
