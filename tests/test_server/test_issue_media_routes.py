"""Hosted issue media over HTTP (SPEC §8.4, §8.12): the raw staging upload, the
range and availability reads, and the dashboard's same-origin session route.

The happy path is proved by the loopback run; these tests are the failure paths and
the contract: what is refused, what is cleaned up, and what limits mean. Media bytes
are built in code (``tests/issue_media_helpers.py``), padded and seeded so every
object has its own hash.
"""

from __future__ import annotations

import hashlib
import http.client
import shutil
import socket
import struct
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.core.ids import generate_issue_id, generate_media_id, generate_op_id
from lattice.core.issue_media import frame_name
from lattice.remote import http as remote_http
from lattice.remote import issue_media as remote_issue_media
from lattice.server import admin
from lattice.server.issue_media import MAX_RANGE_BYTES, available_media, read_media
from lattice.server.testing import ServerHandle, running_server, wait_for
from tests.issue_media_helpers import HTML_AS_PNG, SVG, jpeg, mp4, png
from tests.test_server.conftest import mint
from tests.test_server.web_client import WebClient

SLUG = "alpha"
OCTET = {"Content-Type": "application/octet-stream"}
MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers (shared with test_issue_media_transactions.py)
# ---------------------------------------------------------------------------


def blob(size: int, seed: bytes = b"", *, head: bytes | None = None) -> bytes:
    """A valid synthetic PNG of exactly *size* bytes; *seed* makes its hash unique."""
    if head is not None:
        data = head + seed
        assert len(data) <= size, "the object is too small for its header and seed"
        return data + b"\x00" * (size - len(data))

    base = png()
    prefix = b"profile\0\0"  # iCCP keyword, NUL, and compression method
    extra = size - len(base)
    raw_size = None
    for blocks in range(1, 2 + (size // 65535)):
        candidate = extra - 12 - len(prefix) - 6 - 5 * blocks
        if candidate >= 0 and (candidate + 65534) // 65535 == blocks:
            raw_size = candidate
            break
    if raw_size is None:
        # Small quota fixtures cannot fit a valid iCCP chunk. Use a valid
        # grayscale IDAT stream of the requested size instead.
        signature = b"\x89PNG\r\n\x1a\n"

        def png_chunk(kind: bytes, payload: bytes) -> bytes:
            body = kind + payload
            return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

        for width in range(1, size):
            pixels = (seed + b"\x00" * width)[:width]
            ihdr = struct.pack(">IIBBBBB", width, 1, 8, 0, 0, 0, 0)
            data = (
                signature
                + png_chunk(b"IHDR", ihdr)
                + png_chunk(b"IDAT", zlib.compress(b"\x00" + pixels, level=0))
                + png_chunk(b"IEND", b"")
            )
            if len(data) == size:
                return data
        raise AssertionError("the requested size cannot hold a valid synthetic PNG")

    raw_profile = (seed + b"\x00" * raw_size)[:raw_size]
    compressed = bytearray(b"\x78\x01")
    chunks = [raw_profile[i : i + 65535] for i in range(0, raw_size, 65535)]
    for index, part in enumerate(chunks):
        compressed.append(1 if index == len(chunks) - 1 else 0)
        compressed.extend(struct.pack("<HH", len(part), len(part) ^ 0xFFFF))
        compressed.extend(part)
    if not chunks:  # zlib stream still needs one final empty stored block
        compressed.extend(b"\x01\x00\x00\xff\xff")
    compressed.extend(struct.pack(">I", zlib.adler32(raw_profile)))
    profile = prefix + bytes(compressed)
    chunk = struct.pack(">I", len(profile)) + b"iCCP" + profile
    chunk += struct.pack(">I", zlib.crc32(b"iCCP" + profile))
    ihdr_end = 8 + 25
    data = base[:ihdr_end] + chunk + base[ihdr_end:]
    assert len(data) == size
    return data


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def call(
    server: ServerHandle,
    method: str,
    path: str,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    length: int | str | None = "auto",
) -> tuple[int, dict[str, str], bytes]:
    """One raw request (bytes in, bytes out). *length* ``"auto"`` sends the body's
    own Content-Length, an int declares that instead, ``None`` sends none."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        if token is not None:
            conn.putheader("Authorization", f"Bearer {token}")
        if length == "auto":
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
        elif length is not None:
            conn.putheader("Content-Length", str(length))
        conn.endheaders()
        if body:
            conn.send(body)
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def error_code(body: bytes) -> str:
    import json

    return json.loads(body)["error"]["code"]


def put_stage(
    server: ServerHandle,
    token: str | None,
    data: bytes,
    *,
    digest: str | None = None,
    slug: str = SLUG,
    **kwargs,
) -> tuple[int, dict[str, str], bytes]:
    digest = sha(data) if digest is None else digest
    path = f"/v1/projects/{slug}/issues/media/staging/{digest}"
    return call(server, "PUT", path, token=token, headers=OCTET, body=data, **kwargs)


def stage_ok(server: ServerHandle, token: str, data: bytes, **kwargs) -> dict:
    import json

    status, _, body = put_stage(server, token, data, **kwargs)
    assert status == 201, body
    return json.loads(body)["data"]


def payload(data: bytes, name: str = "shot.png") -> dict:
    """What a hosted ``issue.file`` / ``issue.attach`` item carries for staged bytes."""
    return {"filename": name, "sha256": sha(data), "size": len(data), "staged": True}


def item(data: bytes, name: str = "shot.png", **extra) -> dict:
    return {"payload": payload(data, name), **extra}


def stage_dir(root: Path, slug: str = SLUG) -> Path:
    return root / "projects" / slug / ".runtime" / "issue-media" / "staging"


def manifest_dir(root: Path, slug: str = SLUG) -> Path:
    return root / "projects" / slug / ".runtime" / "issue-media" / "manifests"


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


def board_of(root: Path, slug: str = SLUG) -> Path:
    return root / "projects" / slug / ".lattice"


def file_issue(
    server: ServerHandle, token: str, media: list[dict], *, op_id: str | None = None, **params
) -> tuple[int, dict, dict]:
    """``issue.file`` through the real server; ``(status, headers, parsed body)``."""
    extra = {"op_id": op_id} if op_id else {}
    return server.op(
        SLUG, "issue.file", {"title": "t", "media": media, **params}, token=token, **extra
    )


def filed(server: ServerHandle, token: str, media: list[dict]) -> dict:
    """A filed issue's view (``id``, ``media`` with ids, hashes and frames)."""
    status, _, body = file_issue(server, token, media)
    assert status == 200, body
    return body["data"]["result"]["value"]


@contextmanager
def media_server(root: Path, **limits: int) -> Iterator[ServerHandle]:
    with running_server(root, config={"limits": limits} if limits else None) as handle:
        yield handle


@pytest.fixture()
def root(root: Path) -> Path:
    """The conftest root (two projects, audit off) with the issue log on in both."""
    for slug in (SLUG, "beta"):
        admin.set_project_config(root, slug, {"issues.enabled": True})
    return root


@pytest.fixture()
def token(root: Path) -> str:
    return mint(root, projects=[SLUG])


# ---------------------------------------------------------------------------
# 1. Raw upload: PUT .../issues/media/staging/{sha256}
# ---------------------------------------------------------------------------


def test_upload_needs_a_bearer_token(server: ServerHandle, root: Path) -> None:
    data = blob(200, b"auth")
    assert put_stage(server, None, data)[0] == 401
    assert put_stage(server, "lat_tok_nope_nope", data)[0] == 401
    assert names(stage_dir(root)) == []


def test_upload_needs_a_token_for_the_project(server: ServerHandle, root: Path) -> None:
    """Tokens are scoped by project; the staging route has no weaker rule than the
    operations (there is no read-only token in this protocol)."""
    beta_only = mint(root, projects=["beta"])
    status, _, body = put_stage(server, beta_only, blob(200, b"scope"))
    assert status == 403, body
    assert error_code(body) == "FORBIDDEN"
    assert names(stage_dir(root)) == []
    # The same token may stage into its own project.
    assert put_stage(server, beta_only, blob(200, b"scope"), slug="beta")[0] == 201


@pytest.mark.parametrize("content_type", ["image/jpeg", "image/png"])
def test_photo_upload_returns_raw_and_canonical_hashes_without_alias_records(
    server: ServerHandle, root: Path, token: str, content_type: str
) -> None:
    from tests.photo_metadata_helpers import jpeg_with_gps, png_with_gps
    from lattice.core.issue_media import strip_photo_metadata

    raw = jpeg_with_gps() if content_type == "image/jpeg" else png_with_gps()
    raw_hash = sha(raw)
    clean = strip_photo_metadata(raw, content_type)
    result = stage_ok(server, token, raw)

    assert result["upload_sha256"] == raw_hash
    assert result["sha256"] == sha(clean)
    assert result["size_bytes"] == len(clean)
    assert result["photo_metadata_status"] == "stripped"
    assert (stage_dir(root) / f"{sha(clean)}.blob").read_bytes() == clean
    assert not (stage_dir(root) / "aliases").exists()


def test_upload_finish_does_not_read_or_rewrite_video_bytes(
    server: ServerHandle, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = server.project(SLUG).issue_media
    data = mp4(b"finish-without-a-second-copy")
    upload = manager.begin_upload(sha(data), len(data))
    upload.write(data)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == upload.temporary:
            raise AssertionError("video upload temp file must not be read into memory")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(
        manager,
        "_write_upload_bytes",
        lambda *_args: pytest.fail("video upload temp file must not be rewritten"),
    )

    result = upload.finish()

    assert result["sha256"] == sha(data)
    assert result["size_bytes"] == len(data)
    assert manager._blob_path(sha(data)).stat().st_size == len(data)


def test_board_busy_keeps_an_inflight_canonical_hash_owned_by_another_upload(
    server: ServerHandle, token: str
) -> None:
    from lattice.core.issue_media import strip_photo_metadata
    from tests.photo_metadata_helpers import jpeg_with_gps

    manager = server.project(SLUG).issue_media
    raw = jpeg_with_gps()
    canonical_hash = sha(strip_photo_metadata(raw, "image/jpeg"))
    upload = manager.begin_upload(sha(raw), len(raw))
    upload.write(raw)
    manager._inflight.add(canonical_hash)
    try:
        with pytest.raises(OpError) as raised:
            upload.finish()
        assert raised.value.code == "BOARD_BUSY"
        assert canonical_hash in manager._inflight
    finally:
        manager._inflight.discard(canonical_hash)


@pytest.mark.parametrize("content_type", ["image/jpeg", "image/png"])
def test_raw_photo_stages_are_consumed_by_file_and_attach_with_canonical_dedupe(
    server: ServerHandle, root: Path, token: str, content_type: str
) -> None:
    from lattice.core.issue_media import strip_photo_metadata
    from lattice.storage.issue_media import media_path as stored_media_path
    from tests.photo_metadata_helpers import (
        assert_no_identifying_metadata,
        jpeg_with_gps,
        png_with_gps,
    )

    fixture = jpeg_with_gps if content_type == "image/jpeg" else png_with_gps
    suffix = ".jpg" if content_type == "image/jpeg" else ".png"
    first_raw, second_raw = fixture(gps_value=1), fixture(gps_value=9)
    assert first_raw != second_raw
    first_clean = strip_photo_metadata(first_raw, content_type)
    second_clean = strip_photo_metadata(second_raw, content_type)
    assert first_clean == second_clean

    first_stage = stage_ok(server, token, first_raw)
    assert first_stage["upload_sha256"] == sha(first_raw)
    assert first_stage["sha256"] == sha(first_clean)
    assert first_stage["size_bytes"] == len(first_clean)
    issue = filed(server, token, [item(first_clean, f"first{suffix}")])
    (entry,) = issue["media"]
    stored = stored_media_path(board_of(root), issue["id"], entry)
    assert stored is not None
    assert stored.read_bytes() == first_clean
    assert entry["sha256"] == sha(stored.read_bytes())
    assert entry["size_bytes"] == len(stored.read_bytes())
    assert_no_identifying_metadata(stored.read_bytes(), content_type)

    # The second upload has a distinct raw hash but converges to the same staged
    # object. Attach consumes that stage and skips the already-held canonical hash.
    second_stage = stage_ok(server, token, second_raw)
    assert second_stage["upload_sha256"] == sha(second_raw)
    assert second_stage["sha256"] == first_stage["sha256"]
    assert second_stage["size_bytes"] == first_stage["size_bytes"]
    status, _, response = server.op(
        SLUG,
        "issue.attach",
        {"issue": issue["id"], "media": [item(second_clean, f"second{suffix}")]},
        token=token,
    )
    assert status == 200, response
    attached = response["data"]["result"]["value"]
    assert len(attached["media"]) == 1
    assert attached["last_event_id"] == issue["last_event_id"]
    assert stored.read_bytes() == first_clean
    assert_no_identifying_metadata(stored.read_bytes(), content_type)


def test_legacy_photo_stage_without_status_is_safe_and_reupload_refreshes_metadata(
    server: ServerHandle, root: Path, token: str
) -> None:
    import json

    for data, reupload in ((jpeg(), False), (jpeg(80, 40), True)):
        staged = stage_ok(server, token, data)
        metadata_path = stage_dir(root) / f"{staged['sha256']}.json"
        metadata = json.loads(metadata_path.read_text())
        metadata.pop("photo_metadata_status")
        metadata_path.write_text(json.dumps(metadata))

        if reupload:
            refreshed = stage_ok(server, token, data)
            assert refreshed["photo_metadata_status"] == "stripped"
            metadata = json.loads(metadata_path.read_text())
            assert metadata["photo_metadata_status"] == "stripped"

        issue = filed(server, token, [item(data, "legacy.jpg")])
        assert len(issue["media"]) == 1


def test_filing_rejects_a_staged_video_frame_without_stripped_status(
    server: ServerHandle, root: Path, token: str
) -> None:
    import json

    video = mp4(b"frame-status-guard")
    frame = jpeg()
    stage_ok(server, token, video)
    stage_ok(server, token, frame)
    metadata_path = stage_dir(root) / f"{sha(frame)}.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["photo_metadata_status"] = "not_applicable"
    metadata_path.write_text(json.dumps(metadata))

    status, _, body = file_issue(
        server,
        token,
        [
            item(
                video,
                "clip.mp4",
                video={"width": 64, "height": 48, "duration_ms": 1000},
                frames=[{"t_ms": 500, "payload": payload(frame, "frame.jpg")}],
            )
        ],
    )

    assert status == 400
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("kind", ["jpeg", "png", "heic"])
def test_hosted_photo_keep_fallback_requires_both_steps_and_keeps_only_on_failure(
    server: ServerHandle, root: Path, token: str, kind: str
) -> None:
    import json

    from lattice.core.issue_media import strip_photo_metadata
    from lattice.storage.issue_media import media_path as stored_media_path
    from tests.issue_media_helpers import heic
    from tests.photo_metadata_helpers import jpeg_with_gps
    from lattice.storage.issues import list_issue_snapshots

    name, raw = {
        "jpeg": ("broken.jpg", b"\xff\xd8\xff\xe1\x00\x20Exif"),
        "png": ("broken.png", b"\x89PNG\r\n\x1a\n"),
        "heic": ("kept.heic", heic()),
    }[kind]
    raw_hash = sha(raw)
    raw_stage = stage_dir(root) / f"{raw_hash}.blob"
    raw_metadata = stage_dir(root) / f"{raw_hash}.json"
    raw_reservation = stage_dir(root) / f"{raw_hash}.reserve"

    status, _, body = put_stage(server, token, raw)
    assert status == 400, body
    assert error_code(body) == "VALIDATION_ERROR"
    assert not raw_stage.exists() and not raw_metadata.exists()
    assert not (stage_dir(root) / "aliases").exists()
    assert not raw_reservation.exists()
    assert server.project(SLUG).issue_media._reserved == {}

    status, _, body = call(
        server,
        "PUT",
        f"/v1/projects/{SLUG}/issues/media/staging/{raw_hash}",
        token=token,
        headers={**OCTET, "X-Lattice-Keep-Photo-Metadata": "true"},
        body=raw,
    )
    assert status == 201, body
    staged = json.loads(body)["data"]
    assert staged["photo_metadata_status"] == "kept"
    assert staged["sha256"] == staged["upload_sha256"] == raw_hash
    assert staged["size_bytes"] == len(raw)
    assert raw_stage.read_bytes() == raw

    status, _, refused = file_issue(server, token, [item(raw, name)])
    assert status == 400, refused
    assert refused["error"]["code"] == "VALIDATION_ERROR"
    assert list_issue_snapshots(board_of(root)) == []

    status, _, accepted = file_issue(server, token, [item(raw, name)], keep_photo_metadata=True)
    assert status == 200, accepted
    issue = accepted["data"]["result"]["value"]
    (entry,) = issue["media"]
    stored = stored_media_path(board_of(root), issue["id"], entry)
    assert stored is not None and stored.read_bytes() == raw
    assert entry["sha256"] == raw_hash and entry["size_bytes"] == len(raw)

    # The keep header is a fallback, not a privacy bypass for valid photos.
    valid = jpeg_with_gps()
    clean = strip_photo_metadata(valid, "image/jpeg")
    valid_hash = sha(valid)
    status, _, body = call(
        server,
        "PUT",
        f"/v1/projects/{SLUG}/issues/media/staging/{valid_hash}",
        token=token,
        headers={**OCTET, "X-Lattice-Keep-Photo-Metadata": "true"},
        body=valid,
    )
    assert status == 201, body
    staged_valid = json.loads(body)["data"]
    assert staged_valid["photo_metadata_status"] == "stripped"
    assert staged_valid["upload_sha256"] == valid_hash
    assert staged_valid["sha256"] == sha(clean)
    assert staged_valid["size_bytes"] == len(clean)
    assert (stage_dir(root) / f"{sha(clean)}.blob").read_bytes() == clean
    status, _, filed_valid = file_issue(
        server,
        token,
        [item(clean, "valid.jpg")],
        keep_photo_metadata=True,
    )
    assert status == 200, filed_valid
    valid_issue = filed_valid["data"]["result"]["value"]
    (valid_entry,) = valid_issue["media"]
    valid_stored = stored_media_path(board_of(root), valid_issue["id"], valid_entry)
    assert valid_stored is not None and valid_stored.read_bytes() == clean
    assert valid_entry["sha256"] == sha(clean)


def test_photo_quota_reserves_the_canonical_size_after_raw_upload(root: Path, token: str) -> None:
    from lattice.core.issue_media import strip_photo_metadata
    from tests.photo_metadata_helpers import jpeg_with_gps

    raw = jpeg_with_gps()
    clean = strip_photo_metadata(raw, "image/jpeg")
    spare = len(raw) - len(clean)
    assert spare > 0
    with media_server(root, max_issue_media_project_bytes=len(raw)) as server:
        stage_ok(server, token, raw)
        manager = server.project(SLUG).issue_media
        assert manager._staged_unique_bytes() == len(clean)
        filler = blob(spare, b"canonical quota transfer")
        assert len(filler) == spare
        assert put_stage(server, token, filler)[0] == 201
        assert manager._staged_unique_bytes() == len(raw)


def test_existing_canonical_photo_stage_is_credited_once_during_raw_quota_transfer(
    server: ServerHandle, root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.core.issue_media import strip_photo_metadata
    from tests.photo_metadata_helpers import jpeg_with_gps

    first_raw, second_raw = jpeg_with_gps(1), jpeg_with_gps(9)
    canonical = strip_photo_metadata(first_raw, "image/jpeg")
    canonical_hash = sha(canonical)
    assert strip_photo_metadata(second_raw, "image/jpeg") == canonical
    stage_ok(server, token, first_raw)
    manager = server.project(SLUG).issue_media
    original_write = manager._write_upload_bytes

    def tighten_after_sanitize(path: Path, content: bytes) -> None:
        original_write(path, content)
        if path.suffix == ".part":
            # The second upload's raw reservation already exists. At publication,
            # only the pre-existing canonical staged object should count.
            manager.max_project_bytes = len(canonical)

    monkeypatch.setattr(manager, "_write_upload_bytes", tighten_after_sanitize)
    result = stage_ok(server, token, second_raw)
    assert result["sha256"] == canonical_hash
    assert manager._staged_unique_bytes() == len(canonical)


def _body_complete(reply: bytes) -> bool:
    head, _, body = reply.partition(b"\r\n\r\n")
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return len(body) >= int(value)
    return True


@pytest.mark.parametrize("mode", ["missing", "chunked"])
def test_upload_needs_a_content_length(server: ServerHandle, root: Path, token: str, mode) -> None:
    data = blob(200, b"len")
    path = f"/v1/projects/{SLUG}/issues/media/staging/{sha(data)}"
    if mode == "missing":
        status, _, body = call(server, "PUT", path, token=token, headers=OCTET, length=None)
    else:
        conn = socket.create_connection(("127.0.0.1", server.port), timeout=10)
        try:
            conn.sendall(
                (
                    f"PUT {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n"
                    "Content-Type: application/octet-stream\r\n"
                    "Transfer-Encoding: chunked\r\n\r\n"
                ).encode()
                + f"{len(data):x}\r\n".encode()
                + data
                + b"\r\n0\r\n\r\n"
            )
            reply = b""
            # Headers and body may arrive in separate segments: read to the
            # end of the declared body (the server closes or length-delimits).
            while b"\r\n\r\n" not in reply or not _body_complete(reply):
                part = conn.recv(65536)
                if not part:
                    break
                reply += part
        finally:
            conn.close()
        status, body = int(reply.split()[1]), reply
    assert status == 400, body
    assert b"VALIDATION_ERROR" in body
    assert names(stage_dir(root)) == []


@pytest.mark.parametrize(
    "digest",
    [
        pytest.param("A" * 64, id="uppercase"),
        pytest.param("ab" * 10, id="short"),
        pytest.param("a" * 65, id="long"),
        pytest.param("g" * 64, id="not-hex"),
        pytest.param("." * 64, id="dots"),
        pytest.param("a" * 60 + "%2e%2e", id="encoded-dots"),
    ],
)
def test_the_url_digest_must_be_64_lowercase_hex(
    server: ServerHandle, root: Path, token: str, digest: str
) -> None:
    status, _, body = put_stage(server, token, blob(200, b"url"), digest=digest)
    assert status in (400, 404), body
    assert names(stage_dir(root)) == []
    # Not even the object's real (valid) digest is used when the URL's is not.
    assert not any(stage_dir(root).glob("*.blob")) if stage_dir(root).is_dir() else True


def test_a_digest_that_does_not_match_the_body_is_refused_and_nothing_stays(
    server: ServerHandle, root: Path, token: str
) -> None:
    claimed, actual = blob(300, b"one"), blob(300, b"two")
    status, _, body = put_stage(server, token, actual, digest=sha(claimed))
    assert status == 400, body
    assert error_code(body) == "VALIDATION_ERROR"
    assert names(stage_dir(root)) == []  # no .part, .blob, .json or .reserve
    assert server.project(SLUG).issue_media._reserved == {}


def test_a_body_shorter_than_its_content_length_leaves_nothing(
    server: ServerHandle, root: Path, token: str
) -> None:
    data = blob(300, b"cut")
    path = f"/v1/projects/{SLUG}/issues/media/staging/{sha(data)}"
    conn = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        conn.sendall(
            (
                f"PUT {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n"
                f"Content-Type: application/octet-stream\r\nContent-Length: {len(data)}\r\n\r\n"
            ).encode()
            + data[:100]
        )
        conn.shutdown(socket.SHUT_WR)
        conn.recv(65536)
    finally:
        conn.close()
    assert wait_for(lambda: names(stage_dir(root)) == [])
    assert wait_for(lambda: server.project(SLUG).issue_media._inflight == set())


def test_an_object_over_the_per_file_limit_is_refused_before_its_body_is_read(
    root: Path, token: str
) -> None:
    with media_server(root, max_issue_media_file_bytes=1000) as server:
        data = blob(1001, b"big")
        path = f"/v1/projects/{SLUG}/issues/media/staging/{sha(data)}"
        # Declare 1001 bytes and send none: a server that read the body first would
        # wait for it (the 10 s socket timeout fails this test); this one answers.
        status, _, body = call(
            server, "PUT", path, token=token, headers=OCTET, body=None, length=len(data)
        )
        assert status == 413, body
        assert error_code(body) == "PAYLOAD_TOO_LARGE"
        assert names(stage_dir(root)) == []
        # The limit itself is allowed.
        assert put_stage(server, token, blob(1000, b"fits"))[0] == 201


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"just some text, not media", id="text"),
        pytest.param(HTML_AS_PNG, id="html"),
        pytest.param(SVG, id="svg"),
        pytest.param(b"", id="empty"),
    ],
)
def test_content_that_is_not_photo_or_video_is_refused(
    server: ServerHandle, root: Path, token: str, data: bytes
) -> None:
    status, _, body = put_stage(server, token, data)
    assert status == 400, body
    assert error_code(body) == "VALIDATION_ERROR"
    assert names(stage_dir(root)) == []


def test_repeating_an_upload_is_idempotent(server: ServerHandle, root: Path, token: str) -> None:
    data = blob(400, b"again")
    first = stage_ok(server, token, data)
    second = stage_ok(server, token, data)
    assert (
        first
        == second
        == {
            "upload_sha256": sha(data),
            "sha256": sha(data),
            "size_bytes": 400,
            "content_type": "image/png",
            "photo_metadata_status": "stripped",
            "staged": True,
        }
    )
    assert names(stage_dir(root)) == [f"{sha(data)}.blob", f"{sha(data)}.json"]
    assert (stage_dir(root) / f"{sha(data)}.blob").read_bytes() == data


def test_the_project_quota_counts_in_flight_bytes_once_per_hash_and_published_bytes_per_object(
    root: Path, token: str
) -> None:
    with media_server(root, max_issue_media_project_bytes=500) as server:
        a, b, c = blob(200, b"a"), blob(200, b"b"), blob(200, b"c")
        # A refused upload releases its reservation: it must not eat the quota.
        assert put_stage(server, token, b"x" * 450)[0] == 400
        stage_ok(server, token, a)
        stage_ok(server, token, b)  # 400 of 500 in flight
        status, _, body = put_stage(server, token, c)
        assert status == 413, body
        assert error_code(body) == "MEDIA_QUOTA_EXCEEDED"
        assert sha(c) not in "".join(names(stage_dir(root)))
        # The same hash again is never counted twice.
        assert stage_ok(server, token, a)["size_bytes"] == 200

        # Publish A: its 200 bytes now count as published, not as staged.
        filed(server, token, [item(a)])
        assert server.project(SLUG).issue_media.published_bytes == 200
        # Storage is not deduplicated, so staging a published hash again is a
        # second stored object: 200 published + 200 staged (B) + 200 more.
        status, _, body = put_stage(server, token, a)
        assert status == 413 and error_code(body) == "MEDIA_QUOTA_EXCEEDED", body
        status, _, body = put_stage(server, token, c)
        assert status == 413 and error_code(body) == "MEDIA_QUOTA_EXCEEDED", body


def test_abandoned_staging_expires_and_a_referenced_stage_does_not(
    server: ServerHandle, root: Path, token: str
) -> None:
    manager = server.project(SLUG).issue_media
    abandoned, referenced = blob(300, b"old"), blob(300, b"held")
    for data in (abandoned, referenced):
        stage_ok(server, token, data)
    op_id, issue_id, media_id = generate_op_id(), generate_issue_id(), generate_media_id()
    manager.add_manifest(
        op_id,
        issue_id,
        [
            {
                "media_id": media_id,
                "t_ms": None,
                "sha256": sha(referenced),
                "size_bytes": 300,
                "target": f"issues/media/{issue_id}/{media_id}.png",
            }
        ],
    )
    now = time.time()
    assert manager.expire_staging(now=now + 3600) == 0  # an hour old: kept
    assert manager.expire_staging(now=now + 25 * 3600) == 1  # a day old: only the abandoned
    assert names(stage_dir(root)) == [f"{sha(referenced)}.blob", f"{sha(referenced)}.json"]
    # Once its manifest is gone (the operation aborted), the next sweep takes it.
    manager.abort_operation(op_id)
    assert names(stage_dir(root)) == [] and names(manifest_dir(root)) == []


# ---------------------------------------------------------------------------
# 2. Reads: media, frames, availability, the dashboard route
# ---------------------------------------------------------------------------

PHOTO = blob(3000, b"photo")
BIG = blob(MIB + MIB // 2, b"big")  # 1.5 MiB: more than one range
CLIP = mp4(b"clip" + b"." * 4000)
FRAME = jpeg(64, 48)


@pytest.fixture()
def issue(server: ServerHandle, token: str) -> dict:
    """An issue with a photo, a 1.5 MiB photo, and a video with one frame."""
    for data in (PHOTO, BIG, CLIP, FRAME):
        stage_ok(server, token, data)
    clip = item(
        CLIP,
        "clip.mp4",
        video={"width": 64, "height": 48, "duration_ms": 1500},
        frames=[{"t_ms": 500, "payload": payload(FRAME, "f.jpg")}],
    )
    return filed(server, token, [item(PHOTO), item(BIG, "big.png"), clip])


def media_path(issue: dict, index: int, *, frame: str | None = None) -> str:
    base = f"/v1/projects/{SLUG}/issues/media/{issue['id']}/{issue['media'][index]['id']}"
    return base if frame is None else f"{base}/frames/{frame}"


def get(server, path: str, token: str | None, **headers: str):
    return call(server, "GET", path, token=token, headers=headers)


def test_a_media_read_serves_verified_bytes_with_the_recorded_type(
    server: ServerHandle, token: str, issue: dict
) -> None:
    for index, data, content_type in ((0, PHOTO, "image/png"), (2, CLIP, "video/mp4")):
        entry = issue["media"][index]
        assert entry["content_type"] == content_type  # recorded in the issue's metadata
        status, headers, body = get(server, media_path(issue, index), token)
        assert status == 200
        assert body == data
        assert headers["content-type"].split(";")[0] == content_type
        assert headers["accept-ranges"] == "bytes"
        assert headers["etag"] == f'"{sha(data)}"' == f'"{entry["sha256"]}"'
        assert headers["x-content-type-options"] == "nosniff"
        assert int(headers["content-length"]) == len(data)
    frame = frame_name(500)
    status, headers, body = get(server, media_path(issue, 2, frame=frame), token)
    assert (status, body, headers["content-type"]) == (200, FRAME, "image/jpeg")


@pytest.mark.parametrize(
    ("spec", "start", "end"),
    [
        ("bytes=10-19", 10, 19),
        ("bytes=2990-", 2990, 2999),
        ("bytes=-5", 2995, 2999),
        ("bytes=0-0", 0, 0),
        ("bytes=2999-5000", 2999, 2999),  # an end past the object is trimmed to it
    ],
)
def test_a_valid_range_is_206_with_its_content_range(
    server: ServerHandle, token: str, issue: dict, spec: str, start: int, end: int
) -> None:
    status, headers, body = get(server, media_path(issue, 0), token, Range=spec)
    assert status == 206
    assert body == PHOTO[start : end + 1]
    assert headers["content-range"] == f"bytes {start}-{end}/3000"
    assert headers["accept-ranges"] == "bytes"
    assert headers["content-type"].split(";")[0] == "image/png"
    assert int(headers["content-length"]) == end - start + 1


@pytest.mark.parametrize("spec", ["bytes=3000-", "bytes=5000-6000", "bytes=-0", "bytes=9-3"])
def test_an_unsatisfiable_range_is_416_naming_the_size(
    server: ServerHandle, token: str, issue: dict, spec: str
) -> None:
    status, headers, body = get(server, media_path(issue, 0), token, Range=spec)
    assert status == 416, body
    assert headers["content-range"] == "bytes */3000"
    assert headers["accept-ranges"] == "bytes"
    assert PHOTO[:16] not in body


def test_a_range_is_capped_at_1_mib_and_never_refused_for_its_length(
    server: ServerHandle, token: str, issue: dict
) -> None:
    """The docs cap each range at 1 MiB, as the local dashboard does (it shortens the
    range). A browser's first request for a video is ``bytes=0-``: refusing it with 416
    would stop a larger-than-1-MiB video from ever playing."""
    size = len(BIG)
    path = media_path(issue, 1)
    cases = {
        f"bytes=0-{MAX_RANGE_BYTES - 1}": (0, MAX_RANGE_BYTES - 1),  # exactly the cap
        "bytes=0-": (0, MAX_RANGE_BYTES - 1),  # open-ended: shortened
        f"bytes=0-{MAX_RANGE_BYTES}": (0, MAX_RANGE_BYTES - 1),  # one over: shortened
        f"bytes={MAX_RANGE_BYTES}-": (MAX_RANGE_BYTES, size - 1),  # the rest fits
        "bytes=-1500000": (size - 1500000, size - 1500000 + MAX_RANGE_BYTES - 1),
    }
    for spec, (start, end) in cases.items():
        status, headers, body = get(server, path, token, Range=spec)
        assert status == 206, (spec, status)
        assert headers["content-range"] == f"bytes {start}-{end}/{size}", spec
        assert body == BIG[start : end + 1], spec
        assert len(body) <= MAX_RANGE_BYTES


def test_reads_need_a_token_for_the_project(
    server: ServerHandle, root: Path, token: str, issue: dict
) -> None:
    path = media_path(issue, 0)
    assert get(server, path, None)[0] == 401
    assert get(server, path, "lat_tok_nope_nope")[0] == 401
    beta_only = mint(root, projects=["beta"])
    assert get(server, path, beta_only)[0] == 403
    avail = f"/v1/projects/{SLUG}/issues/media/availability?issue={issue['id']}"
    assert get(server, avail, None)[0] == 401
    assert get(server, avail, beta_only)[0] == 403
    frame_path = media_path(issue, 2, frame=frame_name(500))
    assert get(server, frame_path, None)[0] == 401
    assert get(server, frame_path, beta_only)[0] == 403


@pytest.mark.parametrize(
    "suffix",
    [
        "/med_00000000000000000000000000",  # well-formed, not on this issue
        "/not-a-media-id",
        "/..%2f..%2fconfig.json",
        "/%2e%2e",
    ],
)
def test_unknown_or_hostile_media_ids_are_not_served(
    server: ServerHandle, token: str, issue: dict, suffix: str
) -> None:
    status, _, body = get(server, f"/v1/projects/{SLUG}/issues/media/{issue['id']}{suffix}", token)
    assert status in (400, 404), body
    assert b"hosted" not in body and b"project_code" not in body


@pytest.mark.parametrize("frame", ["t9999.000s.jpg", "../../../config.json", "x.jpg", "t0.5s.jpg"])
def test_a_bad_or_missing_frame_name_is_not_served(
    server: ServerHandle, token: str, issue: dict, frame: str
) -> None:
    status, _, body = get(server, media_path(issue, 2) + "/frames/" + frame, token)
    assert status in (400, 404), body


def _board_copy(server: ServerHandle, tmp_path: Path) -> Path:
    """A copy of the served board's files, for tests that corrupt the disk."""
    target = tmp_path / "copy" / ".lattice"
    shutil.copytree(
        board_of(server.root), target, ignore=shutil.ignore_patterns("hosted", "locks")
    )
    return target


def test_a_symlink_or_a_changed_file_is_never_read(
    server: ServerHandle, tmp_path: Path, issue: dict
) -> None:
    board = _board_copy(server, tmp_path)
    photo = issue["media"][0]
    stored = board / "issues" / "media" / issue["id"] / f"{photo['id']}.png"
    assert read_media(board, issue["id"], photo["id"]).body == PHOTO

    secret = tmp_path / "secret.png"
    secret.write_bytes(PHOTO)  # even bytes with the right hash
    stored.unlink()
    stored.symlink_to(secret)
    with pytest.raises(OpError) as exc:
        read_media(board, issue["id"], photo["id"])
    assert exc.value.code in {"NOT_FOUND", "INTEGRITY_ERROR"}
    assert all(
        row["media_id"] != photo["id"]
        for row in available_media(board, [issue["id"]])["issues"][issue["id"]]
    )

    # A symlinked issue directory is refused too.
    stored.unlink()
    moved = tmp_path / "moved"
    shutil.move(board / "issues" / "media" / issue["id"], moved)
    (board / "issues" / "media" / issue["id"]).symlink_to(moved)
    with pytest.raises(OpError):
        read_media(board, issue["id"], photo["id"])

    # Bytes that no longer match the recorded hash are not served.
    (board / "issues" / "media" / issue["id"]).unlink()
    shutil.move(moved, board / "issues" / "media" / issue["id"])
    stored.write_bytes(blob(3000, b"tampered"))
    with pytest.raises(OpError) as exc:
        read_media(board, issue["id"], photo["id"])
    assert exc.value.code == "INTEGRITY_ERROR"


def test_a_recorded_hash_is_validated_before_it_reaches_a_header(
    server: ServerHandle, tmp_path: Path, issue: dict
) -> None:
    """A snapshot with a bad hash is rebuilt from the log; a log with one is refused:
    either way the string never reaches a path or an ``ETag`` header."""
    import json

    board = _board_copy(server, tmp_path)
    evil = "A" * 63 + '"\r\nX-Evil: 1'
    snapshot_path = board / "issues" / f"{issue['id']}.json"
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["media"][0]["sha256"] = evil
    snapshot_path.write_text(json.dumps(snapshot))
    served = read_media(board, issue["id"], issue["media"][0]["id"])
    assert served.sha256 == sha(PHOTO) and served.body == PHOTO  # healed from the log

    log_path = board / "issues" / "events" / f"{issue['id']}.jsonl"
    lines = [json.loads(x) for x in log_path.read_text().splitlines()]
    added = next(e for e in lines if e["type"] == "issue_media_added")
    added["data"]["sha256"] = evil
    log_path.write_text("".join(json.dumps(e) + "\n" for e in lines))
    snapshot_path.unlink()
    with pytest.raises(OpError):
        read_media(board, issue["id"], issue["media"][0]["id"])
    with pytest.raises(OpError):
        available_media(board, [issue["id"]])


def test_availability_lists_present_objects_for_repeated_issue_parameters(
    server: ServerHandle, token: str, issue: dict
) -> None:
    other_data = blob(500, b"other")
    stage_ok(server, token, other_data)
    other = filed(server, token, [item(other_data)])
    bare = filed(server, token, [])
    unknown = generate_issue_id()
    query = "&".join(f"issue={i}" for i in (issue["id"], other["id"], bare["id"], unknown))
    status, _, body = get(server, f"/v1/projects/{SLUG}/issues/media/availability?{query}", token)
    assert status == 200, body
    import json

    issues = json.loads(body)["data"]["issues"]
    assert set(issues) == {issue["id"], other["id"], bare["id"], unknown}
    assert issues[unknown] == [] and issues[bare["id"]] == []  # unknown ids are ignored
    rows = {row["media_id"]: row for row in issues[issue["id"]]}
    assert set(rows) == {m["id"] for m in issue["media"]}
    photo = rows[issue["media"][0]["id"]]
    assert photo == {
        "media_id": issue["media"][0]["id"],
        "sha256": sha(PHOTO),
        "size_bytes": 3000,
        "content_type": "image/png",
        "frames": [],
    }
    clip = rows[issue["media"][2]["id"]]
    assert clip["frames"] == [{"t_ms": 500, "sha256": sha(FRAME), "size_bytes": len(FRAME)}]
    assert PHOTO[:32] not in body  # metadata only


def test_availability_bounds_agree_with_the_clients_batch_size(
    server: ServerHandle, token: str
) -> None:
    ids = [generate_issue_id() for _ in range(205)]
    base = f"/v1/projects/{SLUG}/issues/media/availability"
    ok = get(server, base + "?" + "&".join(f"issue={i}" for i in ids[:100]), token)
    assert ok[0] == 200
    over = get(server, base + "?" + "&".join(f"issue={i}" for i in ids[:101]), token)
    assert over[0] == 400 and error_code(over[2]) == "VALIDATION_ERROR"
    assert get(server, base, token)[0] == 400  # at least one issue
    assert get(server, base + "?issue=not-an-issue", token)[0] == 400
    # The client sends 100 at a time, so 205 ids are three requests the server accepts.
    remote = remote_http.Remote(alias="t", url=server.url, token=token)
    result = remote_issue_media.availability(remote, SLUG, ids)
    assert result == {i: [] for i in ids}


def test_removed_media_is_reported_absent_and_not_served(
    server: ServerHandle, root: Path, token: str, issue: dict
) -> None:
    gone = issue["media"][0]
    status, _, body = server.op(
        SLUG,
        "issue.detach",
        {"issue": issue["id"], "media": gone["id"], "reason": "r"},
        token=token,
    )
    assert status == 200, body
    import json

    avail = get(
        server, f"/v1/projects/{SLUG}/issues/media/availability?issue={issue['id']}", token
    )
    listed = {row["media_id"] for row in json.loads(avail[2])["data"]["issues"][issue["id"]]}
    assert listed == {m["id"] for m in issue["media"][1:]}
    assert get(server, media_path(issue, 0), token)[0] == 404
    assert not list((board_of(root) / "issues" / "media" / issue["id"]).glob(f"{gone['id']}*"))


# -- the dashboard's session route ------------------------------------------


def _session(server: ServerHandle, token: str) -> tuple[WebClient, dict[str, str]]:
    web = WebClient(server)
    assert web.login(token).status == 303
    return web, {"Cookie": f"lattice_session={web.session}"}


def test_the_dashboard_route_serves_the_same_bytes_to_a_session_only(
    server: ServerHandle, token: str, issue: dict
) -> None:
    _web, cookie = _session(server, token)
    path = f"/p/{SLUG}/issues/media/{issue['id']}/{issue['media'][0]['id']}"
    status, headers, body = call(server, "GET", path, headers=cookie)
    assert (status, body) == (200, PHOTO)
    assert headers["etag"] == f'"{sha(PHOTO)}"' and headers["accept-ranges"] == "bytes"
    status, headers, body = call(server, "GET", path, headers={**cookie, "Range": "bytes=10-19"})
    assert (status, body) == (206, PHOTO[10:20])
    assert headers["content-range"] == "bytes 10-19/3000"
    status, headers, _ = call(server, "GET", path, headers={**cookie, "Range": "bytes=9000-"})
    assert status == 416 and headers["content-range"] == "bytes */3000"
    clip_frame = f"/p/{SLUG}/issues/media/{issue['id']}/{issue['media'][2]['id']}/frames/"
    assert call(server, "GET", clip_frame + frame_name(500), headers=cookie)[2] == FRAME
    # Same-origin browsers send Origin on some media loads; a foreign one is refused.
    same = {**cookie, "Origin": server.url}
    assert call(server, "GET", path, headers=same)[0] == 200
    foreign = {**cookie, "Origin": "http://evil.example"}
    assert call(server, "GET", path, headers=foreign)[0] == 403


def test_the_dashboard_route_refuses_bearer_tokens_and_the_cookie_opens_no_v1_route(
    server: ServerHandle, token: str, issue: dict
) -> None:
    _web, cookie = _session(server, token)
    path = f"/p/{SLUG}/issues/media/{issue['id']}/{issue['media'][0]['id']}"
    assert call(server, "GET", path)[0] == 401  # nothing at all
    status, _, body = call(server, "GET", path, token=token)  # a bearer token alone
    assert status in (401, 403) and PHOTO not in body
    status, _, body = call(server, "GET", path, token=token, headers=cookie)  # both
    assert status == 403 and error_code(body) == "FORBIDDEN"
    # Dashboard cookies never authenticate /v1.
    v1 = media_path(issue, 0)
    assert call(server, "GET", v1, headers=cookie)[0] == 401
    avail = f"/v1/projects/{SLUG}/issues/media/availability?issue={issue['id']}"
    assert call(server, "GET", avail, headers=cookie)[0] == 401
    status, _, _ = call(
        server,
        "PUT",
        f"/v1/projects/{SLUG}/issues/media/staging/{sha(PHOTO)}",
        headers={**OCTET, **cookie},
        body=PHOTO,
    )
    assert status == 401


def test_a_session_for_another_project_cannot_read_this_projects_media(
    server: ServerHandle, root: Path, issue: dict
) -> None:
    _web, cookie = _session(server, mint(root, projects=["beta"]))
    path = f"/p/{SLUG}/issues/media/{issue['id']}/{issue['media'][0]['id']}"
    status, _, body = call(server, "GET", path, headers=cookie)
    assert status in (403, 404) and PHOTO not in body
