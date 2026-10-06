"""LAT-390 reporter-link registry, route, limits, and revoke boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import struct
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import urljoin

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.errors import OpError
from lattice.core.ids import generate_instance_id, generate_op_id
from lattice.ops.task_attach import encode_payload
from lattice.server import admin, app as app_module, control, media_staging, reporter_links, tokens
from lattice.server.issue_media import HostedIssueMedia
from lattice.server.log import _scrub, redact_reporter_path
from lattice.server.testing import ServerHandle, running_server
from lattice.storage.issues import read_issue_events, read_issue_snapshot
from lattice.storage.locks import LockTimeout
from tests.issue_media_helpers import jpeg, mp4, png, webp
from tests.test_server.conftest import NO_AUDIT

PUBLIC_ORIGIN = "https://report.example.test"
PUBLIC_BASE = f"{PUBLIC_ORIGIN}/nested/reports/"


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    root = tmp_path / "server-root"
    admin.init_root(root)
    admin.create_project(root, "alpha", code="ALP")
    admin.create_project(root, "beta", code="BET")
    admin.set_project_config(root, "alpha", {"issues.enabled": True})
    admin.set_project_config(root, "beta", {"issues.enabled": True})
    return root


def create_link(root: Path, *, label: str = "Outside reporter", base: str = PUBLIC_BASE):
    return reporter_links.create_link(root, "alpha", label, base)


def secret_from(url: str) -> str:
    return url.rsplit("/r/", 1)[1].rstrip("/")


def post_report(
    server: ServerHandle,
    secret: str,
    body: dict,
    *,
    origin: str = PUBLIC_ORIGIN,
):
    return server.request(
        "POST",
        f"/r/{secret}/submit",
        body=body,
        headers={"Origin": origin},
    )


def staged_owner(root: Path, digest: str, slug: str = "alpha") -> dict:
    path = root / "projects" / slug / ".runtime" / "issue-media" / "staging" / f"{digest}.json"
    return json.loads(path.read_text())


def png_with_text_metadata() -> bytes:
    raw = png()
    payload = b"Location\x00private location metadata"
    chunk = (
        struct.pack(">I", len(payload))
        + b"tEXt"
        + payload
        + struct.pack(">I", zlib.crc32(b"tEXt" + payload))
    )
    return raw[:33] + chunk + raw[33:]


def webp_with_gps_exif() -> bytes:
    raw = bytearray(webp())
    # VP8X's EXIF-present flag; a minimal GPS IFD in little-endian TIFF.
    raw[20] = 0x08
    gps_offset = 26
    exif = bytearray(b"Exif\x00\x00II*\x00" + struct.pack("<I", 8))
    exif.extend(struct.pack("<H", 1))
    exif.extend(struct.pack("<HHI4s", 0x8825, 4, 1, struct.pack("<I", gps_offset)))
    exif.extend(b"\x00" * 4)
    exif.extend(struct.pack("<H", 1))
    exif.extend(struct.pack("<HHI4s", 1, 2, 2, b"N\x00\x00\x00"))
    exif.extend(b"\x00" * 4)
    chunk = b"EXIF" + struct.pack("<I", len(exif)) + exif
    if len(exif) % 2:
        chunk += b"\x00"
    raw.extend(chunk)
    struct.pack_into("<I", raw, 4, len(raw) - 8)
    return bytes(raw)


def test_cli_create_list_and_revoke_bind_exact_filing_contract(root: Path) -> None:
    runner = CliRunner()
    missing = runner.invoke(
        cli,
        [
            "server",
            "project",
            "reporter-link",
            "create",
            "alpha",
            "--label",
            "Alex",
            "--root",
            str(root),
        ],
    )
    assert missing.exit_code == 2

    created = runner.invoke(
        cli,
        [
            "server",
            "project",
            "reporter-link",
            "create",
            "alpha",
            "--label",
            "human:atin",
            "--public-base-url",
            "https://forms.example.test/prefix/",
            "--root",
            str(root),
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    data = json.loads(created.stdout)["data"]
    record = data["link"]
    secret = secret_from(data["url"])
    assert secret.startswith("rpt_")
    assert data["url"] == f"https://forms.example.test/prefix/r/{secret}/"
    raw_registry = (root / reporter_links.REGISTRY_NAME).read_text()
    assert secret not in raw_registry
    assert record["public_base_url"] == "https://forms.example.test/prefix/"
    assert record["public_origin"] == "https://forms.example.test"

    token_record = next(row for row in tokens._read(root) if row.id == record["token_id"])
    assert token_record.projects == ("alpha",)
    assert token_record.only == ("issue.file",)
    assert token_record.source == f"reporter-link:{record['id']}"
    assert token_record.actors == (reporter_links.SERVICE_ACTOR,)
    assert token_record.ops_per_minute == 30
    assert token_record.bytes_per_minute == 268435456
    assert token_record.max_staged_bytes == 536870912

    listed = runner.invoke(
        cli, ["server", "project", "reporter-link", "list", "--root", str(root), "--json"]
    )
    assert listed.exit_code == 0, listed.output
    assert secret not in listed.stdout
    assert "secret_sha256" not in listed.stdout

    revoked = runner.invoke(
        cli,
        [
            "server",
            "project",
            "reporter-link",
            "revoke",
            record["id"],
            "--root",
            str(root),
            "--json",
        ],
    )
    assert revoked.exit_code == 0, revoked.output
    assert next(row for row in tokens._read(root) if row.id == token_record.id).revoked_at


def test_cli_rejects_impersonation_labels_and_unsafe_bases(root: Path) -> None:
    for label in ("", "   ", "bad\nlabel", "x" * 257):
        with pytest.raises(OpError):
            reporter_links.create_link(root, "alpha", label, PUBLIC_BASE)
    for base in (
        "https://example.test/?q=1",
        "https://example.test/#fragment",
        "https://example.test/a/../b",
        "http://example.test/prefix/",
    ):
        with pytest.raises(OpError):
            reporter_links.validate_public_base_url(base)
    base, origin = reporter_links.validate_public_base_url("http://127.0.0.1:8840/prefix")
    assert base == "http://127.0.0.1:8840/prefix/"
    assert origin == "http://127.0.0.1:8840"


def test_create_rolls_back_token_if_registry_write_fails(root: Path, monkeypatch) -> None:
    def fail(_root, _links):
        raise OSError("disk failed")

    monkeypatch.setattr(reporter_links, "_write_registry", fail)
    before = {row.id for row in tokens._read(root)}
    with pytest.raises(OpError, match="Could not save reporter link"):
        reporter_links.create_link(root, "alpha", "Reporter", PUBLIC_BASE)
    after = tokens._read(root)
    created = [row for row in after if row.id not in before]
    assert len(created) == 1 and created[0].revoked_at is not None


def test_redact_reporter_path_redacts_the_fixed_prefix_secret_directly() -> None:
    secret = "rpt_" + "A" * 43
    assert redact_reporter_path(f"/r/{secret}/submit") == "/r/[redacted]/submit"
    assert redact_reporter_path("/r/not-a-secret/submit") == "/r/not-a-secret/submit"


def test_report_form_proxy_origin_receipt_headers_and_secret_redaction(root: Path) -> None:
    result = create_link(root, label="human:atin")
    record = result["link"]
    secret = secret_from(result["url"])
    with running_server(root, config=NO_AUDIT) as server:
        page_status, headers, page_body = server.request("GET", f"/r/{secret}/")
        assert page_status == 200
        assert headers["cache-control"] == "no-store"
        assert headers["referrer-policy"] == "no-referrer"
        assert "What happened?" in page_body
        assert 'href="./reporter.css"' in page_body
        assert 'src="./reporter.js"' in page_body
        tracked_status, tracked_headers, _ = server.request(
            "GET", f"/r/{secret}/?fbclid=messenger&utm_source=mail"
        )
        assert tracked_status == 200
        assert tracked_headers["cache-control"] == "no-store"
        for name in ("reporter.css", "reporter.js"):
            tracked_asset, _, tracked_body = server.request(
                "GET", f"/r/{secret}/{name}?utm_campaign=report"
            )
            assert tracked_asset == 200 and tracked_body
        redirect_status, _redirect_headers, redirect_body = server.request(
            "GET", f"/r/{secret}?fbclid=messenger"
        )
        assert redirect_status == 200 and "What happened?" in redirect_body
        page_url = f"{record['public_base_url']}r/{secret}/"
        assert urljoin(page_url, "./reporter.css") == (
            f"{record['public_base_url']}r/{secret}/reporter.css"
        )
        assert secret not in page_body
        for name in ("reporter.css", "reporter.js"):
            asset_status, _, asset_body = server.request("GET", f"/r/{secret}/{name}")
            assert asset_status == 200 and asset_body

        javascript = (
            Path(__file__).parents[2]
            / "src"
            / "lattice"
            / "dashboard"
            / "static"
            / "reporter"
            / "reporter.js"
        ).read_text()
        assert "keep_photo_metadata" not in javascript

        source_ref = generate_instance_id().removeprefix("inst_")
        first = post_report(
            server,
            secret,
            {
                "op_id": generate_op_id(),
                "source_ref": source_ref,
                "title": "Leaking pipe",
                "description": "Water near the south door.",
                "reporter_name": "Alex",
                "reporter_email": "alex@example.test",
                "media": [],
            },
            origin=record["public_origin"],
        )
        status, _, receipt = first
        assert status == 200, receipt
        assert set(receipt) == set(reporter_links.RECEIPT_FIELDS)
        stage_map_path = root / reporter_links.STAGE_MAP_NAME
        assert not stage_map_path.exists()

        retry_raw = png(4, 2)
        retry_digest = hashlib.sha256(retry_raw).hexdigest()
        stage_status, _, retry_stage = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{retry_digest}?filename=retry.png",
            body=retry_raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
        assert stage_status == 201
        stage_map = json.loads(stage_map_path.read_text())
        assert stage_map["entries"]
        duplicate_status, _, duplicate = post_report(
            server,
            secret,
            {
                "op_id": generate_op_id(),
                "source_ref": source_ref,
                "title": "Retry after response loss",
                "media": [retry_stage["data"]],
            },
            origin=record["public_origin"],
        )
        assert duplicate_status == 200 and duplicate["deduplicated"] is True
        stage_map = json.loads(stage_map_path.read_text())
        entry = stage_map["entries"][reporter_links._stage_map_key(record["id"], source_ref)]
        assert entry["hashes"] == [retry_digest]
        assert staged_owner(root, retry_digest)["owners"] == [record["token_id"]]
        assert stage_map_path.stat().st_mode & 0o777 == 0o600
        assert receipt["source"] == f"reporter-link:{record['id']}"
        assert receipt["source_ref"] == source_ref
        assert receipt["external"] is True and receipt["deduplicated"] is False

        duplicate_status, _, duplicate = post_report(
            server,
            secret,
            {
                "op_id": generate_op_id(),
                "source_ref": source_ref,
                "title": "Ignored retry title",
                "media": [],
            },
            origin=record["public_origin"],
        )
        assert duplicate_status == 200
        assert duplicate["id"] == receipt["id"]
        assert duplicate["external"] is True and duplicate["deduplicated"] is True

        snapshot = read_issue_snapshot(root / "projects" / "alpha" / ".lattice", receipt["id"])
        assert snapshot["on_behalf_of"] == "human:atin"
        assert snapshot["external"] is True
        events = read_issue_events(root / "projects" / "alpha" / ".lattice", receipt["id"])
        assert events[0]["actor"] == "agent:reporter-link"

        query_submit = server.request(
            "POST",
            f"/r/{secret}/submit?fbclid=messenger",
            body={
                "op_id": generate_op_id(),
                "source_ref": generate_instance_id().removeprefix("inst_"),
                "title": "Query on write",
            },
            headers={"Origin": record["public_origin"]},
        )
        assert query_submit[0] == 404 and query_submit[2] == "Not found.\n"
        tracker_raw = png(4, 2)
        tracker_digest = hashlib.sha256(tracker_raw).hexdigest()
        tracker_upload = server.request(
            "PUT",
            f"/r/{secret}/media/{generate_instance_id().removeprefix('inst_')}/"
            f"{tracker_digest}?filename=photo.png&fbclid=messenger",
            body=tracker_raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
        assert tracker_upload[0] == 404 and tracker_upload[2] == "Not found.\n"

        invalid = server.request(
            "GET",
            f"/r/{secret}/not-a-route",
            token=tokens.create_token(
                root,
                user="human:test",
                machine="filing",
                actors=("agent:filing",),
                projects=("alpha",),
                only=("issue.file",),
                source="guard-test",
            )["token"],
        )
        assert invalid[0] == 404 and invalid[2] == "Not found.\n"
        assert "TOKEN_RESTRICTED" not in str(invalid[2])

        lines = server.log_lines
        request_paths = [row["path"] for row in lines if row.get("event") == "request"]
        assert "/r/[redacted]/" in request_paths
        assert all(secret not in line for line in server.log_stream.getvalue().splitlines())
        assert secret not in _scrub(f"/r/{secret}/submit")
        assert _scrub(secret) == "[redacted]"


def test_public_route_uses_shared_preparation_and_token_owned_media(root: Path) -> None:
    result = create_link(root, label="Facilities")
    record = result["link"]
    secret = secret_from(result["url"])
    raw = png_with_text_metadata()
    digest = hashlib.sha256(raw).hexdigest()
    source_ref = generate_instance_id().removeprefix("inst_")
    with running_server(root, config=NO_AUDIT) as server:
        status, _headers, response = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=sink.png",
            body=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
        assert status == 201, response
        staged = response["data"]
        assert staged["payload"]["staged"] is True
        assert staged["payload"]["sha256"] != digest
        assert staged_owner(root, staged["payload"]["sha256"])["owners"] == [record["token_id"]]

        status, _headers, receipt = post_report(
            server,
            secret,
            {
                "op_id": generate_op_id(),
                "source_ref": source_ref,
                "title": "Photo report",
                "media": [staged],
            },
            origin=record["public_origin"],
        )
        assert status == 200, receipt
        assert set(receipt) == set(reporter_links.RECEIPT_FIELDS)
        stage_map = json.loads((root / reporter_links.STAGE_MAP_NAME).read_text())
        assert not stage_map["entries"]


def test_reporter_refuses_webp_gps_metadata_before_staging(root: Path) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    raw = webp_with_gps_exif()
    digest = hashlib.sha256(raw).hexdigest()
    source_ref = generate_instance_id().removeprefix("inst_")
    with running_server(root, config=NO_AUDIT) as server:
        status, _, refusal = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=location.webp",
            body=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
        assert status == 400
        assert refusal["error"]["code"] == "PHOTO_METADATA_UNSTRIPPED"
        assert (
            refusal["error"]["message"]
            == reporter_links.REPORTER_COPY["PHOTO_METADATA_UNSTRIPPED"]
        )
        project = server.project("alpha")
        assert project.issue_media._read_stage_metadata(digest) is None
    stage_map_path = root / reporter_links.STAGE_MAP_NAME
    assert not stage_map_path.exists() or not json.loads(stage_map_path.read_text())["entries"]


def test_reporter_video_refuses_when_shared_preparation_cannot_strip(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LATTICE_FFMPEG", "off")
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    raw = mp4()
    digest = hashlib.sha256(raw).hexdigest()
    source_ref = generate_instance_id().removeprefix("inst_")
    with running_server(root, config=NO_AUDIT) as server:
        status, _, refusal = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=location.mp4",
            body=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
    assert status == 400
    assert refusal["error"]["code"] == "MEDIA_STAGE_UNAVAILABLE"
    assert "ffmpeg" not in refusal["error"]["message"].lower()
    assert "ask the board admin" not in refusal["error"]["message"].lower()


def test_terminal_failure_keeps_shared_hash_owned_by_sibling_source_ref(root: Path) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    raw = png()
    digest = hashlib.sha256(raw).hexdigest()
    first_ref = generate_instance_id().removeprefix("inst_")
    second_ref = generate_instance_id().removeprefix("inst_")
    with running_server(root, config=NO_AUDIT) as server:
        staged_by_ref = {}
        for source_ref in (first_ref, second_ref):
            status, _, staged = server.request(
                "PUT",
                f"/r/{secret}/media/{source_ref}/{digest}?filename=shared.png",
                body=raw,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Origin": record["public_origin"],
                },
            )
            assert status == 201
            staged_by_ref[source_ref] = staged["data"]

        invalid = staged_by_ref[first_ref]
        invalid["payload"]["sha256"] = "0" * 64
        status, _, refusal = post_report(
            server,
            secret,
            {
                "op_id": generate_op_id(),
                "source_ref": first_ref,
                "title": "Staged media no longer matches",
                "media": [invalid],
            },
            origin=record["public_origin"],
        )
        assert status in {400, 404}
        assert refusal["error"]["code"]

    entries = json.loads((root / reporter_links.STAGE_MAP_NAME).read_text())["entries"]
    assert set(entries) == {reporter_links._stage_map_key(record["id"], second_ref)}
    assert staged_owner(root, digest)["owners"] == [record["token_id"]]


def test_successful_filing_preserves_shared_stage_until_sibling_source_ref_files(
    root: Path,
) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    raw = png()
    digest = hashlib.sha256(raw).hexdigest()
    refs = [generate_instance_id().removeprefix("inst_") for _ in range(2)]
    with running_server(root, config=NO_AUDIT) as server:
        staged_by_ref = {}
        for source_ref in refs:
            status, _, staged = server.request(
                "PUT",
                f"/r/{secret}/media/{source_ref}/{digest}?filename=shared.png",
                body=raw,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Origin": record["public_origin"],
                },
            )
            assert status == 201, staged
            staged_by_ref[source_ref] = staged["data"]

        receipts = []
        for source_ref in refs:
            status, _, receipt = post_report(
                server,
                secret,
                {
                    "op_id": generate_op_id(),
                    "source_ref": source_ref,
                    "title": f"Report {source_ref}",
                    "media": [staged_by_ref[source_ref]],
                },
                origin=record["public_origin"],
            )
            assert status == 200, receipt
            assert receipt["source_ref"] == source_ref
            receipts.append(receipt)
            stage_map = json.loads((root / reporter_links.STAGE_MAP_NAME).read_text())
            if source_ref == refs[0]:
                assert set(stage_map["entries"]) == {
                    reporter_links._stage_map_key(record["id"], refs[1])
                }
                assert server.project("alpha").issue_media._blob_path(digest).is_file()
                assert staged_owner(root, digest)["owners"] == [record["token_id"]]
            else:
                assert stage_map["entries"] == {}
                assert not server.project("alpha").issue_media._blob_path(digest).exists()
        assert receipts[0]["id"] != receipts[1]["id"]


def test_reporter_issue_quota_counts_video_frame_sidecars(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    raw = mp4(b"raw upload")
    clip = mp4(b"prepared original")
    frame = jpeg()
    issue_limit = len(clip) + len(frame) - 1
    source_ref = generate_instance_id().removeprefix("inst_")
    prepared_item = {
        "payload": encode_payload("clip.mp4", clip),
        "video": {"width": 8, "height": 8, "duration_ms": 1000},
        "frames": [{"t_ms": 500, "payload": encode_payload("frame.jpg", frame)}],
    }
    monkeypatch.setattr(
        media_staging,
        "prepare_media_file",
        lambda _filename, _content, **_kwargs: prepared_item,
    )
    config = {
        **NO_AUDIT,
        "limits": {"max_issue_media_issue_bytes": issue_limit},
    }
    with running_server(root, config=config) as server:
        project = server.project("alpha")
        sequence_before = project.journal.head_seq
        digest = hashlib.sha256(raw).hexdigest()
        status, _, staged = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=clip.mp4",
            body=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
        assert status == 201, staged
        stage_data = staged["data"]
        stage_hashes = {
            stage_data["payload"]["sha256"],
            stage_data["frames"][0]["payload"]["sha256"],
        }
        status, _, refusal = post_report(
            server,
            secret,
            {
                "op_id": generate_op_id(),
                "source_ref": source_ref,
                "title": "Video with frame over the limit",
                "media": [stage_data],
            },
            origin=record["public_origin"],
        )
        assert status == 413, refusal
        assert refusal["error"]["code"] == "PAYLOAD_TOO_LARGE"
        assert project.journal.head_seq == sequence_before
        stage_map = json.loads((root / reporter_links.STAGE_MAP_NAME).read_text())
        assert stage_map["entries"] == {}
        assert all(
            project.issue_media._read_stage_metadata(digest) is None for digest in stage_hashes
        )


def _request_headers_without_body(
    server: ServerHandle, method: str, path: str, size: int, extra_headers: dict[str, str]
):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    conn.putrequest(method, path)
    conn.putheader("Content-Length", str(size))
    for name, value in extra_headers.items():
        conn.putheader(name, value)
    conn.endheaders()
    started = time.monotonic()
    try:
        response = conn.getresponse()
        body = response.read()
        elapsed = time.monotonic() - started
        return response.status, response.getheader("Connection"), body, elapsed
    finally:
        conn.close()


def test_unknown_link_and_oversized_upload_close_without_reading_body(root: Path) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    data = png()
    digest = hashlib.sha256(data).hexdigest()
    source_ref = generate_instance_id().removeprefix("inst_")
    headers = {
        "Content-Type": "application/octet-stream",
        "Origin": record["public_origin"],
    }
    with running_server(root, config=NO_AUDIT) as server:
        missing_status, missing_close, missing_body, missing_elapsed = (
            _request_headers_without_body(
                server,
                "PUT",
                f"/r/rpt_{'X' * 43}/media/{source_ref}/{digest}",
                len(data),
                headers,
            )
        )
        assert missing_status == 404 and missing_body == b"Not found.\n"
        assert missing_close == "close" and missing_elapsed < 2

        oversized = server.state.config.limits.max_issue_media_file_bytes + 1
        large_status, large_close, large_body, large_elapsed = _request_headers_without_body(
            server,
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}",
            oversized,
            headers,
        )
        assert large_status == 413 and large_close == "close"
        assert large_elapsed < 2
        assert json.loads(large_body)["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_reporter_stage_checks_issue_availability_before_reading_body(root: Path) -> None:
    admin.set_project_config(root, "alpha", {"issues.enabled": False})
    result = create_link(root)
    secret = secret_from(result["url"])
    source_ref = generate_instance_id().removeprefix("inst_")
    digest = hashlib.sha256(png()).hexdigest()
    with running_server(root, config=NO_AUDIT) as server:
        status, connection, body, elapsed = _request_headers_without_body(
            server,
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=photo.png",
            len(png()),
            {"Content-Type": "application/octet-stream", "Origin": PUBLIC_ORIGIN},
        )
    refusal = json.loads(body)
    assert status == 409 and connection == "close" and elapsed < 2
    assert refusal["error"]["code"] == "ISSUES_DISABLED"
    assert refusal["error"]["message"] == reporter_links.REPORTER_COPY["ISSUES_DISABLED"]


def test_reporter_stage_refuses_unloaded_project_before_reading_body(root: Path) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    source_ref = generate_instance_id().removeprefix("inst_")
    digest = hashlib.sha256(png()).hexdigest()
    with running_server(root, config=NO_AUDIT) as server:
        admin.project_lifecycle(root, "alpha", "unload")
        status, connection, body, elapsed = _request_headers_without_body(
            server,
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=photo.png",
            len(png()),
            {"Content-Type": "application/octet-stream", "Origin": record["public_origin"]},
        )
    refusal = json.loads(body)
    assert status == 503 and connection == "close" and elapsed < 2
    assert refusal["error"]["code"] == "BOARD_UNAVAILABLE"


def test_reporter_upload_hash_is_computed_in_the_media_worker(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    raw = png()
    digest = hashlib.sha256(raw).hexdigest()
    source_ref = generate_instance_id().removeprefix("inst_")
    original_sha256 = hashlib.sha256
    original_prepare = media_staging.prepare_media_file
    hash_threads: list[int] = []
    prepare_threads: list[int] = []
    event_loop_hash_threads: list[int] = []

    def track_sha256(data=b"", *args, **kwargs):
        if isinstance(data, (bytes, bytearray)) and bytes(data) == raw:
            hash_threads.append(threading.get_ident())
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                event_loop_hash_threads.append(threading.get_ident())
        return original_sha256(data, *args, **kwargs)

    def track_prepare(*args, **kwargs):
        prepare_threads.append(threading.get_ident())
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", track_sha256)
    monkeypatch.setattr(media_staging, "prepare_media_file", track_prepare)
    with running_server(root, config=NO_AUDIT) as server:
        status, _, response = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=photo.png",
            body=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
    assert status == 201, response
    assert hash_threads and prepare_threads
    assert hash_threads[-1] == prepare_threads[-1]
    assert event_loop_hash_threads == []


def test_submit_rate_limit_closes_without_draining_the_request_body(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = create_link(root)
    secret = secret_from(result["url"])
    request_body = json.dumps(
        {
            "op_id": generate_op_id(),
            "source_ref": generate_instance_id().removeprefix("inst_"),
            "title": "Rate limited report",
        }
    ).encode()
    with running_server(root, config=NO_AUDIT) as server:

        def reject(_token_id: str) -> None:
            raise OpError("RATE_LIMITED", "raw refusal includes the private token ID")

        monkeypatch.setattr(server.state.limits, "enter", reject)
        status, connection, body, elapsed = _request_headers_without_body(
            server,
            "POST",
            f"/r/{secret}/submit",
            len(request_body),
            {"Content-Type": "application/json", "Origin": PUBLIC_ORIGIN},
        )
    refusal = json.loads(body)
    assert status == 429 and connection == "close" and elapsed < 2
    assert refusal["error"]["code"] == "RATE_LIMITED"
    assert refusal["error"]["message"] == reporter_links.REPORTER_COPY["RATE_LIMITED"]


def test_submit_maps_storage_lock_timeout_to_plain_board_busy(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    source_ref = generate_instance_id().removeprefix("inst_")
    raw = png(4, 2)
    digest = hashlib.sha256(raw).hexdigest()
    with running_server(root, config=NO_AUDIT) as server:
        project = server.project("alpha")
        stage_status, _, stage = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=retry.png",
            body=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
        assert stage_status == 201, stage
        staged = stage["data"]
        original_run_write = project.run_write
        write_attempts = 0

        def timeout_once(write):
            nonlocal write_attempts
            write_attempts += 1
            if write_attempts == 1:
                raise LockTimeout("private storage lock detail")
            return original_run_write(write)

        monkeypatch.setattr(project, "run_write", timeout_once)
        status, _, refusal = post_report(
            server,
            secret,
            {
                "op_id": generate_op_id(),
                "source_ref": source_ref,
                "title": "Busy board",
                "media": [staged],
            },
            origin=record["public_origin"],
        )
        retry_status, _, retry_receipt = post_report(
            server,
            secret,
            {
                "op_id": generate_op_id(),
                "source_ref": source_ref,
                "title": "Retry after busy board",
                "media": [staged],
            },
            origin=record["public_origin"],
        )
    assert status == 503
    assert refusal["error"]["code"] == "BOARD_BUSY"
    assert refusal["error"]["message"] == reporter_links.REPORTER_COPY["BOARD_BUSY"]
    assert "private storage lock detail" not in refusal["error"]["message"]
    assert retry_status == 200, retry_receipt
    assert retry_receipt["deduplicated"] is False
    assert retry_receipt["source_ref"] == source_ref


def test_submit_rechecks_revoke_under_lock_before_committing_body(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    source_ref = generate_instance_id().removeprefix("inst_")
    request_body = json.dumps(
        {"op_id": generate_op_id(), "source_ref": source_ref, "title": "Must not file"}
    ).encode()
    body_reader_entered = threading.Event()
    original_read_body = app_module.read_body

    async def watch_read_body(request, state, token):
        body_reader_entered.set()
        return await original_read_body(request, state, token)

    monkeypatch.setattr(app_module, "read_body", watch_read_body)
    with running_server(root, config=NO_AUDIT) as server:
        project = server.project("alpha")
        sequence_before = project.journal.head_seq
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        conn.putrequest("POST", f"/r/{secret}/submit")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Origin", record["public_origin"])
        conn.putheader("Content-Length", str(len(request_body)))
        conn.endheaders()
        try:
            assert body_reader_entered.wait(3), "submit did not reach body receipt"
            revoked = reporter_links.revoke_link(root, record["id"])
            assert revoked["revoked_at"]
            conn.send(request_body)
            response = conn.getresponse()
            body = response.read()
            assert response.status == 404 and body == b"Not found.\n"
        finally:
            conn.close()

        issue_dir = root / "projects" / "alpha" / ".lattice" / "issues"
        assert list(issue_dir.glob("iss_*.json")) == []
        assert project.journal.head_seq == sequence_before


def test_submit_replay_after_revoke_cannot_file_or_keep_stages(root: Path) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    raw = png()
    digest = hashlib.sha256(raw).hexdigest()
    source_ref = generate_instance_id().removeprefix("inst_")
    op_id = generate_op_id()
    with running_server(root, config=NO_AUDIT) as server:
        project = server.project("alpha")
        stage_status, _, stage = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=report.png",
            body=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
        assert stage_status == 201, stage
        staged = stage["data"]
        media_hashes = reporter_links._submitted_hashes([staged])
        payload = {
            "op_id": op_id,
            "source_ref": source_ref,
            "title": "Filed before revoke",
            "media": [staged],
        }
        status, _, receipt = post_report(server, secret, payload, origin=record["public_origin"])
        assert status == 200, receipt
        issue_dir = root / "projects" / "alpha" / ".lattice" / "issues"
        assert len(list(issue_dir.glob("iss_*.json"))) == 1

        unconsumed_raw = png(5, 3)
        unconsumed_digest = hashlib.sha256(unconsumed_raw).hexdigest()
        unconsumed_source_ref = generate_instance_id().removeprefix("inst_")
        unconsumed_status, _, unconsumed = server.request(
            "PUT",
            f"/r/{secret}/media/{unconsumed_source_ref}/{unconsumed_digest}?filename=pending.png",
            body=unconsumed_raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
        assert unconsumed_status == 201, unconsumed
        media_hashes = reporter_links._submitted_hashes([unconsumed["data"]])
        assert project.issue_media._read_stage_metadata(unconsumed_digest) is not None

        reporter_links.revoke_link(root, record["id"])
        replay_status, _, replay = post_report(
            server, secret, payload, origin=record["public_origin"]
        )
        assert replay_status == 404 and replay == "Not found.\n"
        assert len(list(issue_dir.glob("iss_*.json"))) == 1
        assert all(project.issue_media._read_stage_metadata(item) is None for item in media_hashes)
        stage_map_path = root / reporter_links.STAGE_MAP_NAME
        assert json.loads(stage_map_path.read_text())["entries"] == {}


def test_all_methods_and_filing_bearer_get_generic_reporter_404(root: Path) -> None:
    result = create_link(root)
    secret = secret_from(result["url"])
    filing = tokens.create_token(
        root,
        user="human:test",
        machine="filing",
        actors=("agent:filing",),
        projects=("alpha",),
        only=("issue.file",),
        source="guard-test",
    )["token"]
    with running_server(root, config=NO_AUDIT) as server:
        for path in ("/r", f"/r/{secret}/not-allowed"):
            for method in ("OPTIONS", "TRACE", "PATCH", "DELETE"):
                status, _headers, body = server.request(method, path, token=filing)
                assert status == 404 and body == "Not found.\n"
        status, _, body = server.request("GET", "/r/%C3%A9/")
        assert status == 404 and body == "Not found.\n"
        source_ref = generate_instance_id().removeprefix("inst_")
        digest = hashlib.sha256(png()).hexdigest()
        status, _, body = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=x.png&keep_photo_metadata=true",
            body=png(),
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": PUBLIC_ORIGIN,
            },
        )
        assert status == 404 and body == "Not found.\n"

        status, _, body = post_report(
            server,
            secret,
            {
                "op_id": generate_op_id(),
                "source_ref": generate_instance_id().removeprefix("inst_"),
                "title": "No metadata keep option",
                "keep_photo_metadata": True,
            },
            origin=PUBLIC_ORIGIN,
        )
        assert status == 400
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "keep_photo_metadata" not in body["error"]["message"]


def test_malformed_reporter_media_hash_gets_generic_404(root: Path) -> None:
    result = create_link(root)
    secret = secret_from(result["url"])
    source_ref = generate_instance_id().removeprefix("inst_")
    with running_server(root, config=NO_AUDIT) as server:
        status, _headers, body = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/not-a-sha256",
            body=b"ignored",
            headers={"Origin": result["link"]["public_origin"]},
        )
    assert status == 404 and body == "Not found.\n"


def test_reporter_errors_are_static_copy_and_31st_op_is_friendly(root: Path) -> None:
    message = reporter_links._plain_error(
        OpError("MEDIA_STAGE_UNAVAILABLE", "install ffmpeg using the secret CLI command")
    )
    assert "ffmpeg" not in message.message and "CLI" not in message.message
    for code in (
        "PHOTO_METADATA_UNSTRIPPED",
        "MEDIA_STAGE_UNAVAILABLE",
        "CONFLICT",
        "BOARD_BUSY",
        "ISSUES_DISABLED",
        "RATE_LIMITED",
        "PAYLOAD_TOO_LARGE",
        "MEDIA_QUOTA_EXCEEDED",
        "UNKNOWN_INTERNAL_CODE",
    ):
        mapped = reporter_links._plain_error(
            OpError(
                code, "raw server detail names X-Lattice-Keep-Photo-Metadata", {"retry_after": 7}
            )
        )
        assert "X-Lattice-Keep-Photo-Metadata" not in mapped.message
        assert mapped.details["retry_after"] == 7
    result = create_link(root)
    secret = secret_from(result["url"])
    with running_server(root, config=NO_AUDIT) as server:
        for index in range(30):
            status, _, _ = post_report(
                server,
                secret,
                {"op_id": generate_op_id(), "source_ref": "bad", "title": "x"},
            )
            assert status == 400
        status, _headers, body = post_report(
            server,
            secret,
            {"op_id": generate_op_id(), "source_ref": "bad", "title": "x"},
        )
        assert status == 429
        assert body["error"]["code"] == "RATE_LIMITED"
        assert "Too many uploads or reports" in body["error"]["message"]


def test_reporter_media_inflight_limit_uses_safe_copy(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    source_ref = generate_instance_id().removeprefix("inst_")
    raw = png()
    digest = hashlib.sha256(raw).hexdigest()
    with running_server(root, config=NO_AUDIT) as server:
        token_id = record["token_id"]

        def reject_inflight(token: str) -> None:
            raise OpError(
                "RATE_LIMITED",
                f"token {token} already has 8 requests in flight (limit 8)",
                {"retry_after": 1},
            )

        monkeypatch.setattr(server.state.limits, "enter", reject_inflight)
        status, headers, refusal = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{digest}?filename=photo.png",
            body=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
        assert status == 429
        assert headers.get("retry-after") == "1"
        assert refusal["error"]["code"] == "RATE_LIMITED"
        assert refusal["error"]["message"] == reporter_links.REPORTER_COPY["RATE_LIMITED"]
        assert token_id not in refusal["error"]["message"]
        assert "already has 8 requests" not in refusal["error"]["message"]


def _stage_direct(media: HostedIssueMedia, token_id: str, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    upload = media.begin_upload(digest, len(data), token_id=token_id, max_staged_bytes=536870912)
    upload.write(data)
    upload.finish()
    return digest


def test_running_server_revoke_cleans_through_control_and_preserves_other_owner(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = create_link(root)
    record = result["link"]

    def offline_cleanup_must_not_run(*_args) -> None:
        pytest.fail("live-server revoke must clean through its control action")

    monkeypatch.setattr(reporter_links, "_offline_cleanup", offline_cleanup_must_not_run)
    with running_server(root, config=NO_AUDIT) as server:
        project = server.project("alpha")
        other = tokens.create_token(
            root,
            user="human:other",
            machine="other",
            actors=("agent:other",),
            projects=("alpha",),
        )["record"]["id"]
        digest = _stage_direct(project.issue_media, record["token_id"], png())
        # A shared content hash remains available to the other live owner.
        upload = project.issue_media.begin_upload(
            digest,
            project.issue_media._read_stage_metadata(digest)["size_bytes"],
            token_id=other,
            max_staged_bytes=536870912,
        )
        upload.write(png())
        upload.finish()
        revoked = reporter_links.revoke_link(root, record["id"])
        assert revoked["revoked_at"]
        assert staged_owner(root, digest)["owners"] == [other]
        assert project.issue_media._blob_path(digest).is_file()
        assert control.server_running(root)


def test_revoke_on_live_server_with_unloaded_project_returns_pending_cleanup(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = create_link(root)
    record = result["link"]

    def offline_cleanup_must_not_run(*_args) -> None:
        pytest.fail("a running server owns cleanup even when its project is unloaded")

    monkeypatch.setattr(reporter_links, "_offline_cleanup", offline_cleanup_must_not_run)
    with running_server(root, config=NO_AUDIT) as server:
        project = server.project("alpha")
        digest = _stage_direct(project.issue_media, record["token_id"], png())
        admin.project_lifecycle(root, "alpha", "unload")
        revoked = CliRunner().invoke(
            cli,
            [
                "server",
                "project",
                "reporter-link",
                "revoke",
                record["id"],
                "--root",
                str(root),
                "--json",
            ],
        )
        assert revoked.exit_code == 0, revoked.output
        data = json.loads(revoked.stdout)["data"]
        assert data["revoked_at"]
        assert data["cleanup"] == "pending"
        token = next(row for row in tokens._read(root) if row.id == record["token_id"])
        assert token.revoked_at
        assert project.issue_media._read_stage_metadata(digest) is not None
        non_json = CliRunner().invoke(
            cli,
            [
                "server",
                "project",
                "reporter-link",
                "revoke",
                record["id"],
                "--root",
                str(root),
            ],
        )
        assert non_json.exit_code == 0, non_json.output
        assert "cleanup is pending" in non_json.output.lower()


def test_stopped_server_revoke_cleans_under_server_lock_lease(root: Path, monkeypatch) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    source_ref = generate_instance_id().removeprefix("inst_")
    raw = png()
    raw_digest = hashlib.sha256(raw).hexdigest()
    with running_server(root, config=NO_AUDIT) as server:
        status, _, staged = server.request(
            "PUT",
            f"/r/{secret}/media/{source_ref}/{raw_digest}?filename=offline.png",
            body=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": record["public_origin"],
            },
        )
        assert status == 201
        digest = staged["data"]["payload"]["sha256"]
        assert json.loads((root / reporter_links.STAGE_MAP_NAME).read_text())["entries"]

    original_cleanup = reporter_links._offline_cleanup
    lock_probes: list[int | None] = []

    def verify_lease(locked_root: Path, linked_record: dict) -> None:
        lock_probes.append(control.try_server_lock(locked_root))
        original_cleanup(locked_root, linked_record)

    monkeypatch.setattr(reporter_links, "_offline_cleanup", verify_lease)
    reporter_links.revoke_link(root, record["id"])
    assert lock_probes == [None]
    assert not (
        root / "projects" / "alpha" / ".runtime" / "issue-media" / "staging" / f"{digest}.json"
    ).exists()
    assert not json.loads((root / reporter_links.STAGE_MAP_NAME).read_text())["entries"]


def test_slow_upload_does_not_hold_link_lock_or_delay_revoke(root: Path) -> None:
    result = create_link(root)
    record = result["link"]
    secret = secret_from(result["url"])
    data = png()
    digest = hashlib.sha256(data).hexdigest()
    source_ref = generate_instance_id().removeprefix("inst_")
    with running_server(root, config=NO_AUDIT) as server:
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        conn.putrequest("PUT", f"/r/{secret}/media/{source_ref}/{digest}?filename=slow.png")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Origin", record["public_origin"])
        conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        try:
            time.sleep(0.15)
            started = time.monotonic()
            reporter_links.revoke_link(root, record["id"])
            elapsed = time.monotonic() - started
            assert elapsed < 5
            conn.send(data)
            response = conn.getresponse()
            assert response.status == 404
            assert response.read() == b"Not found.\n"
        finally:
            conn.close()
