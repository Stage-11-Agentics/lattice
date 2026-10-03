"""LAT-368 repair 1: media GETs stream from disk in bounded chunks (SPEC §8.12).

Neither an unranged nor a ranged read holds the whole object in memory; both
routes (bearer and dashboard session) send byte-identical bodies with the same
headers as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.server import admin
from lattice.server import issue_media as media_module
from tests.test_server.conftest import mint
from tests.test_server.test_issue_media_routes import (
    SLUG,
    _session,
    blob,
    call,
    filed,
    item,
    media_server,
    sha,
    stage_ok,
)

CHUNK = 1000


@pytest.fixture()
def root(root: Path) -> Path:
    admin.set_project_config(root, SLUG, {"issues.enabled": True})
    return root


@pytest.fixture()
def reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The length of every body read the media routes make."""
    monkeypatch.setattr(media_module, "STREAM_CHUNK", CHUNK)
    lengths: list[int] = []
    real = media_module._pread_chunk

    def spy(fd: int, length: int, offset: int) -> bytes:
        lengths.append(length)
        return real(fd, length, offset)

    monkeypatch.setattr(media_module, "_pread_chunk", spy)
    return lengths


def test_a_media_get_streams_the_object_in_bounded_chunks(root: Path, reads: list[int]) -> None:
    token = mint(root, projects=[SLUG])
    data = blob(10_500, b"stream")
    with media_server(root) as server:
        stage_ok(server, token, data)
        issue = filed(server, token, [item(data)])
        path = f"/v1/projects/{SLUG}/issues/media/{issue['id']}/{issue['media'][0]['id']}"

        reads.clear()
        status, headers, body = call(server, "GET", path, token=token)
        assert (status, body) == (200, data)
        assert headers["content-length"] == str(len(data))
        assert headers["etag"] == f'"{sha(data)}"'
        assert headers["accept-ranges"] == "bytes"
        assert headers["x-content-type-options"] == "nosniff"
        assert "content-range" not in headers
        assert len(reads) == 11 and max(reads) <= CHUNK  # never the whole object

        reads.clear()
        status, headers, body = call(
            server, "GET", path, token=token, headers={"Range": "bytes=2500-6999"}
        )
        assert (status, body) == (206, data[2500:7000])
        assert headers["content-range"] == f"bytes 2500-6999/{len(data)}"
        assert headers["content-length"] == "4500"
        assert len(reads) == 5 and max(reads) <= CHUNK

        # The dashboard's session route streams the same way.
        _web, cookie = _session(server, token)
        reads.clear()
        dash = f"/p/{SLUG}/issues/media/{issue['id']}/{issue['media'][0]['id']}"
        status, headers, body = call(server, "GET", dash, headers=cookie)
        assert (status, body) == (200, data)
        assert headers["content-length"] == str(len(data)) and max(reads) <= CHUNK


def test_a_file_replaced_after_verification_is_not_streamed(root: Path) -> None:
    """A file replaced after verification is refused mid-stream rather than served."""
    token = mint(root, projects=[SLUG])
    data = blob(3000, b"swap")
    with media_server(root) as server:
        stage_ok(server, token, data)
        issue = filed(server, token, [item(data)])
        board = server.project(SLUG).board
        plan = media_module.plan_media_read(board, issue["id"], issue["media"][0]["id"])
        stream = media_module.open_media(plan)
        assert (stream.start, stream.length, stream.status) == (0, 3000, 200)
        changed = bytearray(data)
        changed[0] ^= 1
        plan.path.unlink()
        plan.path.write_bytes(bytes(changed))
        with pytest.raises(media_module.OpError) as caught:
            b"".join(media_module.iter_media(stream))
        assert caught.value.code == "INTEGRITY_ERROR"
