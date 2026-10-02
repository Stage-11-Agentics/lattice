"""The hosted issue-media client cache: verification, modes, atomic fetch, LRU,
sweeping, availability batching, offline degradation, and the view fields.

Unit tests drive :mod:`lattice.remote.issue_media` against a fake transport;
the end-to-end tests (``hosted_env``: a real loopback server and bound
checkouts) prove what the CLI shows and what ordinary sync never does.
SPEC §9.4 "Hosted issue media cache"; api.md "Issue media upload,
availability, and reads".
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from lattice.core.errors import OpError
from lattice.core.issue_media import frame_name
from lattice.remote import cache_paths, http, issue_media
from lattice.server import admin
from tests.issue_media_helpers import jpeg, png
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli

PROJECT = "demo"
CACHE = Path(".lattice") / "cache" / "issue-media"


def _id(kind: str, value: int) -> str:
    return f"{kind}_{value:026d}"


REMOTE = http.Remote(alias="test", url="https://lattice.invalid", token="secret")


# ---------------------------------------------------------------------------
# A fake transport and small builders
# ---------------------------------------------------------------------------


@dataclass
class Media:
    """One stored original: its bytes and the snapshot / server rows describing it."""

    issue_id: str
    media_id: str
    content: bytes
    content_type: str = "image/png"
    frames: tuple[tuple[int, bytes], ...] = ()

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def name(self) -> str:
        return f"{self.media_id}.png"

    def entry(self) -> dict:
        """The snapshot's media entry, as ``issue_views`` gives it."""
        return {
            "id": self.media_id,
            "content_type": self.content_type,
            "sha256": self.sha,
            "size_bytes": len(self.content),
            "removed": False,
            "frames": [],
        }

    def row(self) -> dict:
        """What the server's availability endpoint says about it."""
        return {
            "media_id": self.media_id,
            "sha256": self.sha,
            "size_bytes": len(self.content),
            "content_type": self.content_type,
            "frames": [
                {
                    "t_ms": t_ms,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
                for t_ms, data in self.frames
            ],
        }

    def view(self) -> dict:
        return {"id": self.issue_id, "media": [self.entry()]}

    def endpoint(self, frame: int | None = None) -> str:
        base = f"/v1/projects/{PROJECT}/issues/media/{self.issue_id}/{self.media_id}"
        return base if frame is None else f"{base}/frames/{frame_name(frame)}"


class FakeServer:
    """Stands in for the availability endpoint and the byte reads."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rows: dict[str, list[dict]] = {}
        self.objects: dict[str, bytes] = {}
        self.requests: list[str] = []
        self.unreachable = False
        monkeypatch.setattr(issue_media, "availability", self._availability)
        monkeypatch.setattr(issue_media.http, "request", self._request)

    def publish(self, media: Media) -> None:
        self.rows.setdefault(media.issue_id, []).append(media.row())
        self.objects[media.endpoint()] = media.content
        for t_ms, data in media.frames:
            self.objects[media.endpoint(t_ms)] = data

    def _availability(self, _remote, _project, issue_ids):  # noqa: ANN001, ANN202
        if self.unreachable:
            raise OpError("SERVER_UNREACHABLE", "offline")
        return {issue_id: list(self.rows.get(issue_id, [])) for issue_id in issue_ids}

    def _request(self, _remote, method, path, **_kwargs):  # noqa: ANN001, ANN202
        assert method == "GET"
        self.requests.append(path)
        if self.unreachable:
            raise http.Unreachable("connection refused")
        if path not in self.objects:
            raise http.ServerError("NOT_FOUND", "no such media", None, status=404)
        return http.Response(200, {}, self.objects[path])


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / ".lattice").mkdir(parents=True)
    return checkout


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    return FakeServer(monkeypatch)


def cache_files(root: Path) -> list[Path]:
    base = root / CACHE
    return sorted(p for p in base.rglob("*") if not p.is_dir()) if base.exists() else []


def cache_names(root: Path) -> set[str]:
    return {p.name for p in cache_files(root)}


def seed(root: Path, media: Media, *, data: bytes | None = None) -> Path:
    return issue_media._safe_write(
        root,
        PROJECT,
        media.issue_id,
        media.media_id,
        media.name,
        media.content if data is None else data,
    )


def seed_frame(root: Path, media: Media, t_ms: int, data: bytes) -> Path:
    name = frame_name(t_ms)
    path = issue_media._safe_write(
        root, PROJECT, media.issue_id, media.media_id, name, data, frame=True
    )
    issue_media._write_frame_metadata(
        root,
        PROJECT,
        media.issue_id,
        media.media_id,
        name,
        t_ms,
        hashlib.sha256(data).hexdigest(),
        len(data),
    )
    return path


def annotate(root: Path, *views: dict) -> list[dict]:
    return issue_media.annotate_views(root, REMOTE, PROJECT, list(views))


def assert_view_fields(entry: dict) -> None:
    """The contract: ``path`` is a verified cache file or null; ``available`` is
    local, remote, missing or unreachable (not cached, server not reachable);
    ``missing`` is true exactly when it is missing."""
    for item in (entry, *entry["frames"]):
        assert item["available"] in {"local", "remote", "missing", "unreachable"}
        assert item["missing"] is (item["available"] == "missing")
        if item["available"] == "local":
            assert item["path"] is not None and Path(item["path"]).is_file()
            assert hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"]
            assert len(Path(item["path"]).read_bytes()) == item["size_bytes"]
        else:
            assert item["path"] is None


# ---------------------------------------------------------------------------
# Verification before a cached path is returned
# ---------------------------------------------------------------------------


def test_a_cached_original_is_served_only_after_hash_and_size_verification(
    root: Path, server: FakeServer
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png())
    path = seed(root, media)
    server.unreachable = True

    entry = annotate(root, media.view())[0]["media"][0]

    assert entry["available"] == "local" and entry["path"] == str(path)
    assert_view_fields(entry)


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda c: b"X" * len(c), id="same-size-other-bytes"),
        pytest.param(lambda c: c[:-1], id="truncated"),
        pytest.param(lambda c: c + b"\x00", id="padded"),
        pytest.param(lambda c: b"", id="empty"),
    ],
)
@pytest.mark.parametrize("reachable", [False, True], ids=["offline", "online"])
def test_a_corrupted_cached_file_is_never_returned(
    root: Path, server: FakeServer, corrupt, reachable: bool
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png())
    seed(root, media, data=corrupt(media.content))
    if reachable:
        server.publish(media)
    server.unreachable = not reachable

    entry = annotate(root, media.view())[0]["media"][0]

    assert entry["path"] is None
    assert entry["available"] == ("remote" if reachable else "unreachable")
    assert_view_fields(entry)


def test_a_corrupted_cached_file_is_replaced_by_a_verified_refetch(
    root: Path, server: FakeServer
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png())
    server.publish(media)
    stale = seed(root, media, data=b"X" * len(media.content))

    entry = issue_media.fetch_view_media(root, REMOTE, PROJECT, media.view())["media"][0]

    assert entry["available"] == "local" and entry["path"] == str(stale)
    assert stale.read_bytes() == media.content
    assert server.requests == [media.endpoint()]


@pytest.mark.parametrize(
    "bad",
    [pytest.param(b"Y" * 68, id="wrong-bytes"), pytest.param(b"short", id="wrong-size")],
)
def test_a_download_that_fails_verification_is_refused_and_never_cached(
    root: Path, server: FakeServer, bad: bytes
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png())
    server.publish(media)
    server.objects[media.endpoint()] = bad

    with pytest.raises(OpError) as raised:
        issue_media.fetch_view_media(root, REMOTE, PROJECT, media.view())

    assert raised.value.code == "INTEGRITY_ERROR"
    assert cache_files(root) == []


def test_a_symlinked_cache_object_is_not_returned_and_does_not_crash_a_read(
    root: Path, server: FakeServer, tmp_path: Path
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png())
    server.publish(media)
    outside = tmp_path / "outside.png"
    outside.write_bytes(media.content)  # even the right bytes: it is not our file
    directory = root / CACHE / PROJECT / media.issue_id / media.media_id
    directory.mkdir(parents=True)
    (directory / media.name).symlink_to(outside)

    entry = annotate(root, media.view())[0]["media"][0]

    assert entry["path"] is None and entry["available"] == "remote"
    assert_view_fields(entry)


def test_an_unsafe_media_id_is_missing_never_a_path(root: Path, server: FakeServer) -> None:
    entry = Media(_id("iss", 1), "../../escape", png()).entry()

    result = annotate(root, {"id": _id("iss", 1), "media": [entry]})[0]["media"][0]

    assert result["path"] is None and result["available"] == "missing" and result["missing"]
    assert cache_files(root) == []


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def test_cache_directories_are_0700_and_files_0600_whatever_the_umask(
    root: Path, server: FakeServer
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png(), frames=((1500, jpeg()),))
    server.publish(media)
    previous = os.umask(0)
    try:
        issue_media.fetch_view_media(root, REMOTE, PROJECT, media.view())
    finally:
        os.umask(previous)

    files = cache_files(root)
    assert {p.name for p in files} >= {media.name, frame_name(1500), f"{frame_name(1500)}.meta"}
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    for directory in (root / CACHE, *(p for p in (root / CACHE).rglob("*") if p.is_dir())):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


# ---------------------------------------------------------------------------
# Atomic fetch
# ---------------------------------------------------------------------------


def test_a_download_that_dies_midway_leaves_no_partial_file(
    root: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png())
    server.publish(media)

    def dies(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise http.Unreachable("connection reset", sent=True)

    monkeypatch.setattr(issue_media.http, "request", dies)
    with pytest.raises(OpError) as raised:
        issue_media.fetch_view_media(root, REMOTE, PROJECT, media.view())

    assert raised.value.code == "SERVER_UNREACHABLE"
    assert cache_files(root) == []


def test_a_failed_write_keeps_the_old_file_and_leaves_no_temporary(
    root: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png())
    server.publish(media)
    stale = seed(root, media, data=b"old bytes")

    def no_rename(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise OSError("disk full")

    monkeypatch.setattr(cache_paths.os, "replace", no_rename)
    with pytest.raises(OSError, match="disk full"):
        issue_media.fetch_view_media(root, REMOTE, PROJECT, media.view())
    monkeypatch.undo()

    assert stale.read_bytes() == b"old bytes"
    assert cache_names(root) == {media.name}


# ---------------------------------------------------------------------------
# LRU cap
# ---------------------------------------------------------------------------


def _aged(root: Path, media: Media, age: int) -> Path:
    """Seed *media* with a recorded last use *age* seconds after the epoch."""
    path = seed(root, media, data=b"o" * len(media.content))
    os.utime(path, ns=(age * 1_000_000_000, age * 1_000_000_000))
    return path


def test_the_cap_evicts_the_least_recently_used_entries_first(
    root: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = Media(_id("iss", 1), _id("med", 9), png())
    server.publish(fresh)
    size = len(fresh.content)
    oldest, middle, newest = (Media(_id("iss", 1), _id("med", n), png()) for n in (1, 2, 3))
    paths = [_aged(root, m, age) for m, age in ((newest, 3000), (oldest, 1000), (middle, 2000))]
    monkeypatch.setattr(issue_media, "MAX_CACHE_BYTES", 3 * size)

    result = issue_media.fetch_view_media(root, REMOTE, PROJECT, fresh.view())["media"][0]

    # Four entries, room for three: only the least recently used goes.
    assert not paths[1].exists()
    assert paths[0].exists() and paths[2].exists() and Path(result["path"]).is_file()


def test_a_cache_hit_counts_as_use(
    root: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = Media(_id("iss", 1), _id("med", 9), png())
    server.publish(fresh)
    used = Media(_id("iss", 1), _id("med", 1), png(2, 2))
    other = Media(_id("iss", 1), _id("med", 2), png(3, 3))
    used_path = seed(root, used)
    other_path = seed(root, other)
    os.utime(used_path, ns=(1_000_000_000, 1_000_000_000))  # the older of the two...
    os.utime(other_path, ns=(2_000_000_000, 2_000_000_000))
    server.publish(used)
    # ...until a read verifies it.
    assert annotate(root, used.view())[0]["media"][0]["available"] == "local"
    monkeypatch.setattr(issue_media, "MAX_CACHE_BYTES", len(fresh.content) + len(used.content))

    issue_media.fetch_view_media(root, REMOTE, PROJECT, fresh.view())

    assert used_path.exists() and not other_path.exists()


def test_the_entry_just_fetched_is_never_evicted(
    root: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png(), frames=((0, jpeg()),))
    server.publish(media)
    older = Media(_id("iss", 1), _id("med", 2), png(5, 5))
    older_path = _aged(root, older, 1000)
    monkeypatch.setattr(issue_media, "MAX_CACHE_BYTES", 1)  # smaller than any one entry

    result = issue_media.fetch_view_media(root, REMOTE, PROJECT, media.view())["media"][0]

    assert not older_path.exists()
    assert_view_fields(result)
    assert result["available"] == "local" and result["frames"][0]["available"] == "local"


# ---------------------------------------------------------------------------
# Sweeping removed media
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reachable", [True, False], ids=["online", "offline"])
def test_a_removed_entry_yields_no_path_and_its_cache_is_swept(
    root: Path, server: FakeServer, reachable: bool
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png(), frames=((0, jpeg()),))
    kept = Media(_id("iss", 1), _id("med", 2), png(4, 4))
    seed(root, media)
    seed_frame(root, media, 0, jpeg())
    kept_path = seed(root, kept)
    server.publish(kept)
    server.unreachable = not reachable
    removed = {**media.entry(), "removed": True}

    view = annotate(root, {"id": media.issue_id, "media": [removed, kept.entry()]})[0]

    gone, alive = view["media"]
    assert gone["path"] is None and gone["available"] == "missing" and gone["missing"]
    assert gone["frames"] == []
    assert not (root / CACHE / PROJECT / media.issue_id / media.media_id).exists()
    assert alive["path"] == str(kept_path)


def test_a_cached_copy_the_server_no_longer_has_is_swept_only_when_the_server_answered(
    root: Path, server: FakeServer
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png())
    stale = seed(root, media, data=b"not what the snapshot names")

    server.unreachable = True
    offline = annotate(root, media.view())[0]["media"][0]
    # Nothing learned: nothing swept, and the server is said to be unreachable.
    assert offline["available"] == "unreachable" and not offline["missing"] and stale.exists()

    server.unreachable = False  # the server answers and does not list it
    (root / ".lattice" / "cache" / "unreachable_until").unlink()  # past the offline window
    online = annotate(root, media.view())[0]["media"][0]
    assert online["available"] == "missing" and online["path"] is None
    assert not stale.exists()


# ---------------------------------------------------------------------------
# Availability batching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "batches"),
    [(1, [1]), (100, [100]), (101, [100, 1]), (250, [100, 100, 50])],
)
def test_availability_batches_at_one_hundred_and_merges_every_answer(
    monkeypatch: pytest.MonkeyPatch, count: int, batches: list[int]
) -> None:
    ids = [_id("iss", n) for n in range(count)]
    calls: list[list[str]] = []

    def fake_get_json(_remote, path, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        batch = parse_qs(urlsplit(path).query)["issue"]
        calls.append(batch)
        return {"issues": {i: [{"media_id": _id("med", int(i[-6:]))}] for i in batch}}

    monkeypatch.setattr(issue_media, "get_json", fake_get_json)

    result = issue_media.availability(REMOTE, PROJECT, ids)

    assert [len(batch) for batch in calls] == batches
    assert [i for batch in calls for i in batch] == ids
    assert set(result) == set(ids)
    assert all(result[i] == [{"media_id": _id("med", int(i[-6:]))}] for i in ids)


def test_annotating_two_hundred_and_fifty_issues_pairs_each_with_its_own_answer(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    medias = [Media(_id("iss", n), _id("med", n), png(1 + n % 7, 2)) for n in range(250)]
    by_issue = {m.issue_id: m for m in medias}
    calls: list[int] = []

    def fake_get_json(_remote, path, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        batch = parse_qs(urlsplit(path).query)["issue"]
        calls.append(len(batch))
        rows = {}
        for issue_id in batch:
            row = by_issue[issue_id].row()
            if issue_id == _id("iss", 137):
                row["sha256"] = "0" * 64  # the server holds different bytes under this one
            rows[issue_id] = [row]
        return {"issues": rows}

    monkeypatch.setattr(issue_media, "get_json", fake_get_json)

    views = annotate(root, *(m.view() for m in medias))

    assert calls == [100, 100, 50]
    states = {v["id"]: v["media"][0]["available"] for v in views}
    assert states.pop(_id("iss", 137)) == "missing"
    assert set(states.values()) == {"remote"}


def test_availability_refuses_an_invalid_id_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("no request may be made")

    monkeypatch.setattr(issue_media, "get_json", never)
    with pytest.raises(OpError) as raised:
        issue_media.availability(REMOTE, PROJECT, [_id("iss", 1), "iss_../x"])
    assert raised.value.code == "INTEGRITY_ERROR"


@pytest.mark.parametrize("shape", ["missing-id", "extra-id", "not-a-list"])
def test_availability_refuses_an_answer_that_does_not_match_the_question(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    asked = [_id("iss", 1), _id("iss", 2)]
    answers = {
        "missing-id": {"issues": {asked[0]: []}},
        "extra-id": {"issues": {asked[0]: [], asked[1]: [], _id("iss", 3): []}},
        "not-a-list": {"issues": {asked[0]: [], asked[1]: {}}},
    }
    monkeypatch.setattr(issue_media, "get_json", lambda *_a, **_k: answers[shape])
    with pytest.raises(OpError) as raised:
        issue_media.availability(REMOTE, PROJECT, asked)
    assert raised.value.code == "INTEGRITY_ERROR"


# ---------------------------------------------------------------------------
# Offline degradation and the view fields
# ---------------------------------------------------------------------------


def test_an_unreachable_server_degrades_each_entry_without_raising(
    root: Path, server: FakeServer
) -> None:
    cached = Media(_id("iss", 1), _id("med", 1), png(), frames=((250, jpeg()),))
    absent = Media(_id("iss", 1), _id("med", 2), png(4, 4))
    seed(root, cached)
    seed_frame(root, cached, 250, jpeg())
    server.unreachable = True

    view = annotate(root, {"id": cached.issue_id, "media": [cached.entry(), absent.entry()]})[0]

    have, lack = view["media"]
    assert have["available"] == "local" and have["missing"] is False
    assert [f["available"] for f in have["frames"]] == ["local"]
    assert lack["available"] == "unreachable" and not lack["missing"] and lack["path"] is None
    for entry in view["media"]:
        assert_view_fields(entry)


def test_a_reachable_server_makes_uncached_media_and_frames_remote(
    root: Path, server: FakeServer
) -> None:
    media = Media(_id("iss", 1), _id("med", 1), png(), frames=((0, jpeg()), (900, jpeg(8, 8))))
    server.publish(media)
    seed_frame(root, media, 900, jpeg(8, 8))  # one frame already fetched

    entry = annotate(root, media.view())[0]["media"][0]

    assert (entry["available"], entry["path"], entry["missing"]) == ("remote", None, False)
    assert [(f["t_ms"], f["available"]) for f in entry["frames"]] == [
        (0, "remote"),
        (900, "local"),
    ]
    assert_view_fields(entry)


# ---------------------------------------------------------------------------
# End to end: a real server, two bound checkouts
# ---------------------------------------------------------------------------


@pytest.fixture()
def two(hosted_env: HostedEnv, tmp_path: Path) -> tuple[HostedEnv, Path, Path]:
    admin.set_project_config(hosted_env.server_root, PROJECT, {"issues.enabled": True})
    checkouts = []
    for name in ("a", "b"):
        repo = make_repo(tmp_path / name / "repo")
        assert run_cli(repo, "remote", "attach", "team", PROJECT).exit_code == 0
        checkouts.append(repo)
    return hosted_env, checkouts[0], checkouts[1]


def data_of(repo: Path, *args: str) -> dict:
    result = run_cli(repo, *args, "--json", *(["--actor", "agent:t"] if _writes(args) else []))
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["data"]


def _writes(args: tuple[str, ...]) -> bool:
    return len(args) > 1 and args[0] == "issue" and args[1] in {"file", "detach", "attach"}


def file_with_photo(repo: Path, tmp_path: Path) -> tuple[str, bytes]:
    shot = tmp_path / "shot.png"
    content = png(40, 30)
    shot.write_bytes(content)
    view = data_of(repo, "issue", "file", "Broken", "--evidence", str(shot))
    return view["id"], content


def test_sync_never_fills_the_media_cache_and_a_read_reports_remote(
    two: tuple[HostedEnv, Path, Path], tmp_path: Path
) -> None:
    _env, a, b = two
    issue_id, content = file_with_photo(a, tmp_path)

    # B syncs (any command does), lists, and shows: no media byte arrives.
    assert run_cli(b, "list").exit_code == 0
    listed = data_of(b, "issue", "list")
    shown = data_of(b, "issue", "show", issue_id)

    for repo in (a, b):
        assert cache_files(repo) == []
        assert not (repo / ".lattice" / "issues" / "media").exists()
    for entry in (listed[0]["media"][0], shown["media"][0]):
        assert (entry["available"], entry["path"], entry["missing"]) == ("remote", None, False)

    # Asking for paths is what fetches, verified, into the private cache.
    result = run_cli(b, "issue", "media", issue_id, "--paths")
    assert result.exit_code == 0, result.output
    (printed,) = result.stdout.splitlines()
    assert Path(printed).read_bytes() == content
    assert Path(printed).is_relative_to(b / CACHE / PROJECT)
    assert cache_files(a) == []  # the filer's own checkout fetched nothing
    entry = data_of(b, "issue", "show", issue_id)["media"][0]
    assert (entry["available"], entry["path"], entry["missing"]) == ("local", printed, False)


def test_cache_clear_removes_the_media_cache_and_it_refills_on_demand(
    two: tuple[HostedEnv, Path, Path], tmp_path: Path
) -> None:
    _env, a, b = two
    issue_id, content = file_with_photo(a, tmp_path)
    assert run_cli(b, "issue", "media", issue_id, "--paths").exit_code == 0
    assert cache_files(b)

    cleared = run_cli(b, "cache", "clear")
    assert cleared.exit_code == 0, cleared.output
    assert not (b / CACHE).exists()

    again = run_cli(b, "issue", "media", issue_id, "--paths")
    assert again.exit_code == 0, again.output
    assert Path(again.stdout.strip()).read_bytes() == content


def test_media_removed_elsewhere_never_yields_a_stale_path_and_is_swept(
    two: tuple[HostedEnv, Path, Path], tmp_path: Path
) -> None:
    _env, a, b = two
    issue_id, _content = file_with_photo(a, tmp_path)
    media_id = data_of(a, "issue", "show", issue_id)["media"][0]["id"]
    assert run_cli(b, "issue", "media", issue_id, "--paths").exit_code == 0
    cached = b / CACHE / PROJECT / issue_id / media_id
    assert cached.is_dir()

    detached = run_cli(
        a, "issue", "detach", issue_id, "1", "--reason", "pii", "--actor", "agent:t"
    )
    assert detached.exit_code == 0, detached.output

    shown = data_of(b, "issue", "show", issue_id)["media"][0]
    assert shown["removed"]
    assert shown["path"] is None and shown["available"] == "missing" and shown["missing"]
    assert not cached.exists()
    paths = run_cli(b, "issue", "media", issue_id, "--paths")
    assert paths.exit_code == 0 and paths.stdout == ""
