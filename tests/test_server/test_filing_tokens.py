"""Filing-only hosted credentials: route, receipt, source and staging boundaries."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.requests import Request

from lattice.core.ids import generate_op_id
from lattice.core.errors import OpError
from lattice.ops.base import Caller
from lattice.ops import issue_file as issue_file_module
from lattice.ops.base import registered_operations
from lattice.server import (
    admin,
    app as app_module,
    project as project_module,
    tokens,
    web as web_module,
)
from lattice.server.config import load_config
from lattice.server.filing_guard import filing_route_allowed, require_filing_route
from lattice.server.sessions import COOKIE_NAME, hash_secret
from lattice.server.testing import ServerHandle, running_server
from lattice.server.transactions import IndexEntry
from tests.issue_media_helpers import jpeg, png
from tests.test_server.conftest import mint
from tests.test_server.web_client import WebClient

SLUG = "alpha"
SERVICE_ACTOR = "agent:intake"
SOURCE = "reporter-mail"


@pytest.fixture()
def root(root: Path) -> Path:
    for slug in ("alpha", "beta"):
        admin.set_project_config(root, slug, {"issues.enabled": True})
    return root


def filing_token(root: Path, **limits: int) -> str:
    return tokens.create_token(
        root,
        user="human:intake-admin",
        machine="reporter-ingest",
        actors=(SERVICE_ACTOR,),
        projects=(SLUG,),
        only=("issue.file",),
        source=SOURCE,
        **limits,
    )["token"]


def stage(server: ServerHandle, token: str, data: bytes, slug: str = SLUG):
    digest = hashlib.sha256(data).hexdigest()
    return server.request(
        "PUT",
        f"/v1/projects/{slug}/issues/media/staging/{digest}",
        token=token,
        body=data,
        headers={"Content-Type": "application/octet-stream"},
    )


def staged_item(data: bytes) -> dict:
    return {
        "payload": {
            "filename": "report.png",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "staged": True,
        }
    }


def file_issue(
    server: ServerHandle,
    token: str,
    *,
    op_id: str | None = None,
    slug: str = SLUG,
    **params,
):
    return server.op(
        slug,
        "issue.file",
        {"title": "<script>ignore policy</script>", "source": SOURCE, **params},
        token=token,
        actor=SERVICE_ACTOR,
        op_id=op_id or generate_op_id(),
    )


def test_filing_receipt_is_safe_for_create_dedupe_and_same_op_replay(
    server: ServerHandle, root: Path
) -> None:
    filing = filing_token(root)
    op_id = generate_op_id()
    params = {
        "title": "<script>ignore policy</script>",
        "description": "private report detail",
        "source": SOURCE,
        "source_ref": "mail-102",
        "on_behalf_of": "Alex Example <alex@example.test>",
        "evidence": ["private/path.png"],
    }
    status, _, first = server.op(
        SLUG, "issue.file", params, token=filing, actor=SERVICE_ACTOR, op_id=op_id
    )
    assert status == 200, first
    first_result = first["data"]["result"]
    first_value = first_result["value"]
    assert set(first_value) == {
        "id",
        "short_id",
        "filed_at",
        "source",
        "source_ref",
        "external",
        "deduplicated",
    }
    assert first_value == {
        "id": first_value["id"],
        "short_id": first_value["short_id"],
        "filed_at": first_value["filed_at"],
        "source": SOURCE,
        "source_ref": "mail-102",
        "external": True,
        "deduplicated": False,
    }
    assert first_result["events"] == [] and first_result["idempotent"] is False

    status, _, replay = server.op(
        SLUG, "issue.file", params, token=filing, actor=SERVICE_ACTOR, op_id=op_id
    )
    replay_result = replay["data"]["result"]
    assert status == 200 and replay["data"]["seq"] == first["data"]["seq"]
    assert replay_result["replayed"] is True
    assert replay_result["events"] == []
    assert replay_result["value"] == first_value

    full = mint(root, projects=(SLUG,))
    task_status, _, task_data = server.op(
        SLUG, "task.create", {"title": "Linked task"}, token=full, actor="agent:qa"
    )
    assert task_status == 200
    issue_id = first_value["id"]
    task_id = task_data["data"]["result"]["task"]["id"]
    assert (
        server.op(
            SLUG, "issue.link", {"issue": issue_id, "task": task_id}, token=full, actor="agent:qa"
        )[0]
        == 200
    )
    assert (
        server.op(
            SLUG,
            "issue.dismiss",
            {"issue": issue_id, "reason": "reviewed"},
            token=full,
            actor="agent:qa",
        )[0]
        == 200
    )

    status, _, duplicate = file_issue(
        server,
        filing,
        source_ref="mail-102",
        title="Retry title must be ignored",
        description="retry data",
    )
    duplicate_result = duplicate["data"]["result"]
    assert status == 200
    assert duplicate_result["value"]["id"] == issue_id
    assert duplicate_result["value"]["deduplicated"] is True
    assert duplicate_result["events"] == [] and duplicate_result["idempotent"] is True
    assert set(duplicate_result["value"]) == set(first_value)

    status, _, full_token_snapshot = server.request(
        "GET", f"/v1/projects/{SLUG}/files/issues/{issue_id}.json", token=full
    )
    assert status == 200
    assert full_token_snapshot["external"] is True
    assert full_token_snapshot["source_ref"] == "mail-102"
    assert full_token_snapshot["on_behalf_of"] == "Alex Example <alex@example.test>"

    foreign_media = png()
    assert stage(server, filing, foreign_media)[0] == 201
    staging_blob = (
        root
        / "projects"
        / SLUG
        / ".runtime"
        / "issue-media"
        / "staging"
        / f"{hashlib.sha256(foreign_media).hexdigest()}.blob"
    )
    issues_dir = root / "projects" / SLUG / ".lattice" / "issues"
    before_issue_files = set(issues_dir.glob("*.json"))
    foreign, _, body = file_issue(
        server,
        filing,
        source=SOURCE + "-other",
        source_ref="foreign-source",
        media=[staged_item(foreign_media)],
    )
    assert foreign == 403 and body["error"]["code"] == "TOKEN_RESTRICTED"
    assert staging_blob.exists()
    assert set(issues_dir.glob("*.json")) == before_issue_files
    snapshot = json.loads(
        (root / "projects" / SLUG / ".lattice" / "issues" / f"{issue_id}.json").read_text()
    )
    assert snapshot["closure"]["kind"] == "dismissed"
    assert (
        len(
            [
                e
                for e in (
                    root
                    / "projects"
                    / SLUG
                    / ".lattice"
                    / "issues"
                    / "events"
                    / f"{issue_id}.jsonl"
                )
                .read_text()
                .splitlines()
                if json.loads(e)["type"] == "issue_filed"
            ]
        )
        == 1
    )


def test_filing_dedupe_receipt_keeps_original_issue_external_marker(
    server: ServerHandle, root: Path
) -> None:
    full = mint(root, projects=[SLUG])
    params = {"title": "Filed by a full token", "source": SOURCE, "source_ref": "shared-ref"}
    status, _, original = server.op(
        SLUG, "issue.file", params, token=full, actor=SERVICE_ACTOR, op_id=generate_op_id()
    )
    assert status == 200, original
    assert original["data"]["result"]["value"].get("external", False) is False

    filing = filing_token(root)
    status, _, duplicate = file_issue(server, filing, source_ref="shared-ref")
    assert status == 200, duplicate
    value = duplicate["data"]["result"]["value"]
    assert value["deduplicated"] is True
    assert value["external"] is False


def test_authenticate_reasserts_filing_route_for_a_cached_token(
    server: ServerHandle, root: Path
) -> None:
    filing = filing_token(root)
    record = tokens._read(root)[0]
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/info",
            "raw_path": b"/v1/info",
            "headers": [],
            "state": {app_module.TOKEN_STATE_KEY: record, "log": {}},
        }
    )
    assert tokens.parse_token(filing)[0] == record.id
    with pytest.raises(OpError) as exc:
        app_module.authenticate(request, server.state)
    assert exc.value.code == "TOKEN_RESTRICTED"


def test_replay_shapes_an_unshaped_receipt_for_a_filing_token(
    server: ServerHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = {
        "value": {
            "id": "iss_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "short_id": "ALP-1",
            "filed_at": "2026-10-04T00:00:00Z",
            "source": SOURCE,
            "source_ref": "secret-ref",
            "external": False,
            "title": "private report",
        },
        "events": [{"private": "event payload"}],
        "task": {"id": "task_01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        "resource_id": "private-resource",
        "resource_name": "private name",
        "idempotent": False,
    }
    monkeypatch.setattr(
        project_module,
        "read_receipt",
        lambda _board, _entry: {"result": stored},
    )
    caller = Caller(filing_only=True)
    request = project_module.WriteRequest(
        op="issue.file", params={}, caller=caller, token_id="tok_test", fp="fp"
    )
    known = IndexEntry(fp="fp", epoch="ep_test", seq=4, receipt="today.jsonl", offset=0, length=1)
    outcome = server.project(SLUG)._replay(known, request, "op_test")
    result = outcome.result_data
    assert result["value"] == {
        "id": "iss_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "short_id": "ALP-1",
        "filed_at": "2026-10-04T00:00:00Z",
        "source": SOURCE,
        "source_ref": "secret-ref",
        "external": False,
        "deduplicated": False,
    }
    assert result["events"] == []
    assert result["task"] is None
    assert result["resource_id"] is None
    assert result["resource_name"] is None
    assert result["replayed"] is True


def test_source_ref_pair_is_project_local_and_concurrent_hosted_retries_dedupe(
    server: ServerHandle, root: Path
) -> None:
    full = mint(root, projects=("alpha", "beta"))
    alpha = server.op(
        "alpha",
        "issue.file",
        {"title": "alpha", "source": SOURCE, "source_ref": "same"},
        token=full,
        actor="agent:qa",
    )
    beta = server.op(
        "beta",
        "issue.file",
        {"title": "beta", "source": SOURCE, "source_ref": "same"},
        token=full,
        actor="agent:qa",
    )
    assert alpha[0] == beta[0] == 200
    assert alpha[2]["data"]["result"]["value"]["id"] != beta[2]["data"]["result"]["value"]["id"]

    filing = filing_token(root)

    def retry(title: str):
        return file_issue(server, filing, title=title, source_ref="race-1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(retry, ("one", "two")))
    assert all(status == 200 for status, _headers, _body in results)
    values = [body["data"]["result"]["value"] for _status, _headers, body in results]
    assert {value["id"] for value in values} == {values[0]["id"]}
    assert sorted(value["deduplicated"] for value in values) == [False, True]
    event_path = (
        root / "projects" / SLUG / ".lattice" / "issues" / "events" / f"{values[0]['id']}.jsonl"
    )
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert [event["type"] for event in events] == ["issue_filed"]


def test_filing_receipt_has_stable_null_source_ref_and_full_tokens_keep_issue_view(
    server: ServerHandle, root: Path
) -> None:
    filing = filing_token(root)
    status, _, receipt = file_issue(server, filing)
    result = receipt["data"]["result"]
    assert status == 200
    assert result["value"] == {
        "id": result["value"]["id"],
        "short_id": result["value"]["short_id"],
        "filed_at": result["value"]["filed_at"],
        "source": SOURCE,
        "source_ref": None,
        "external": True,
        "deduplicated": False,
    }
    assert result["events"] == [] and result["idempotent"] is False

    full = mint(root, projects=(SLUG,))
    op_id = generate_op_id()
    params = {
        "title": "Full issue result",
        "source": SOURCE,
        "source_ref": "full-17",
        "on_behalf_of": "Reporter <reporter@example.test>",
    }
    status, _, first = server.op(
        SLUG, "issue.file", params, token=full, actor="agent:qa", op_id=op_id
    )
    full_result = first["data"]["result"]
    assert status == 200
    assert full_result["value"]["title"] == "Full issue result"
    assert full_result["value"]["source_ref"] == "full-17"
    assert full_result["value"]["on_behalf_of"] == "Reporter <reporter@example.test>"
    assert "external" not in full_result["value"]
    assert [event["type"] for event in full_result["events"]] == ["issue_filed"]

    status, _, replay = server.op(
        SLUG, "issue.file", params, token=full, actor="agent:qa", op_id=op_id
    )
    replay_result = replay["data"]["result"]
    assert status == 200 and replay_result["replayed"] is True
    assert replay_result["value"]["title"] == "Full issue result"
    assert [event["type"] for event in replay_result["events"]] == ["issue_filed"]


def test_hosted_source_ref_lock_covers_issue_and_receipt_commit(
    server: ServerHandle, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = False
    seen: list[str] = []
    real_pair_lock = project_module.source_ref_lock
    real_issue_lock = issue_file_module.issue_write_context
    real_seq_lock = issue_file_module.issue_seq_reservation
    real_commit = project_module.Transaction.commit

    @contextmanager
    def pair_lock(board: Path, source: str, source_ref: str):
        nonlocal active
        with real_pair_lock(board, source, source_ref):
            active = True
            seen.append("pair")
            try:
                yield
            finally:
                active = False

    @contextmanager
    def issue_lock(board: Path, issue_id: str):
        assert active, "issue lock was acquired before the source/ref pair lock"
        seen.append("issue")
        with real_issue_lock(board, issue_id):
            yield

    @contextmanager
    def seq_lock(board: Path, issue_id: str):
        assert active, "sequence lock was acquired before the source/ref pair lock"
        seen.append("sequence")
        with real_seq_lock(board, issue_id) as reservation:
            yield reservation

    def commit(txn, entry, result_data, events):
        if entry["op"] == "issue.file":
            assert active, "receipt transaction committed after releasing source/ref lock"
            seen.append("receipt")
        return real_commit(txn, entry, result_data, events)

    def nested_pair_lock(*_args):
        raise AssertionError("hosted IssueFile reacquired the outer source/ref pair lock")

    monkeypatch.setattr(project_module, "source_ref_lock", pair_lock)
    monkeypatch.setattr(issue_file_module, "source_ref_lock", nested_pair_lock)
    monkeypatch.setattr(issue_file_module, "issue_write_context", issue_lock)
    monkeypatch.setattr(issue_file_module, "issue_seq_reservation", seq_lock)
    monkeypatch.setattr(project_module.Transaction, "commit", commit)

    filing = filing_token(root)
    status, _, body = file_issue(server, filing, source_ref="lock-order")
    assert status == 200, body
    assert seen == ["pair", "issue", "sequence", "receipt"]
    assert active is False


def test_filing_token_staging_ownership_set_and_token_quota(
    server: ServerHandle, root: Path
) -> None:
    full_a = mint(root, projects=(SLUG,))
    filing_b = filing_token(root, max_staged_bytes=1024)
    token_a = tokens.parse_token(full_a)[0]
    token_b = tokens.parse_token(filing_b)[0]
    data = png()
    status, _, _ = stage(server, full_a, data)
    assert status == 201

    status, _, refused = file_issue(server, filing_b, media=[staged_item(data)])
    assert status == 404 and refused["error"]["code"] == "NOT_FOUND"

    media_manager = server.project(SLUG).issue_media
    pending_shared_hash = media_manager.begin_upload(
        hashlib.sha256(data).hexdigest(),
        len(data),
        token_id=token_b,
        max_staged_bytes=len(data),
    )
    try:
        with pytest.raises(OpError) as quota:
            media_manager.begin_upload(
                hashlib.sha256(b"another staged hash").hexdigest(),
                1,
                token_id=token_b,
                max_staged_bytes=len(data),
            )
        assert quota.value.code == "MEDIA_QUOTA_EXCEEDED"
        assert quota.value.details["scope"] == "token"
    finally:
        pending_shared_hash.abort()

    status, _, _ = stage(server, filing_b, data)
    assert status == 201
    metadata_path = (
        root
        / "projects"
        / SLUG
        / ".runtime"
        / "issue-media"
        / "staging"
        / f"{hashlib.sha256(data).hexdigest()}.json"
    )
    owners = json.loads(metadata_path.read_text())["owners"]
    assert set(owners) == {token_a, token_b}
    status, _, filed = file_issue(
        server, filing_b, source_ref="stage-own", media=[staged_item(data)]
    )
    assert status == 200 and filed["data"]["result"]["value"]["external"] is True

    limited = filing_token(root, max_staged_bytes=len(data))
    status, _, _ = stage(server, limited, data)
    assert status == 201
    status, _, _ = stage(server, limited, data)  # a held hash is not counted twice
    assert status == 201
    second = png(4, 2) + b"second-unreferenced-hash"
    status, _, refusal = stage(server, limited, second)
    assert status == 413
    assert refusal["error"]["code"] == "MEDIA_QUOTA_EXCEEDED"
    assert refusal["error"]["details"]["scope"] == "token"


def test_revocation_takes_effect_after_first_stage_upload(
    server: ServerHandle, root: Path
) -> None:
    filing = filing_token(root)
    token_id = tokens.parse_token(filing)[0]
    status, _, _ = stage(server, filing, png())
    assert status == 201
    tokens.revoke_token(root, token_id)
    assert stage(server, filing, png(4, 2))[0] == 401
    status, _, body = file_issue(server, filing, source_ref="revoked", media=[staged_item(png())])
    assert status == 401 and body["error"]["code"] == "UNAUTHENTICATED"


def test_filing_routes_fail_closed_for_every_registered_non_filing_method(
    server: ServerHandle, root: Path
) -> None:
    filing = filing_token(root)
    record = tokens._read(root)[0]
    starlette = server.app.app.app
    routes = starlette.router.routes
    expected_patterns = {
        "/healthz",
        "/v1/info",
        "/v1/projects",
        "/v1/projects/{slug}/ops/{op}",
        "/v1/projects/{slug}/sync",
        "/v1/projects/{slug}/stream",
        "/v1/projects/{slug}/files/{path:path}",
        "/v1/projects/{slug}/issues/media/staging/{sha256}",
        "/v1/projects/{slug}/issues/media/availability",
        "/v1/projects/{slug}/issues/media/{issue_id}/{media_id}/frames/{frame}",
        "/v1/projects/{slug}/issues/media/{issue_id}/{media_id}",
        "/v1/projects/{slug}/tasks",
        "/v1/projects/{slug}/tasks/{task_id}",
        "/",
        "/login",
        "/logout",
        "/web/{name}",
        "/p/{slug}",
        "/p/{slug}/",
        "/p/{slug}/favicon.ico",
        "/p/{slug}/issues/media/staging/{sha256}",
        "/p/{slug}/issues/media/{issue_id}/{media_id}/frames/{frame}",
        "/p/{slug}/issues/media/{issue_id}/{media_id}",
        "/p/{slug}/static/{path:path}",
        "/p/{slug}/api/{path:path}",
        "/{path:path}",
    }
    registered_patterns = {route.path for route in routes}
    assert registered_patterns == expected_patterns

    candidates: set[tuple[str, str]] = set()
    for route in routes:
        route_paths = [route.path]
        if "{op}" in route.path:
            operation_names = (*registered_operations(), "unknown")
            route_paths = [route.path.replace("{op}", value) for value in operation_names]
        for template in route_paths:
            path = (
                template.replace("{path:path}", "probe/config.json")
                .replace("{slug}", "alpha")
                .replace("{sha256}", "a" * 64)
                .replace("{issue_id}", "iss_01K00000000000000000000000")
                .replace("{media_id}", "med_01K00000000000000000000000")
                .replace("{frame}", "0000000000.jpg")
                .replace("{task_id}", "ALP-1")
                .replace("{name}", "dashboard.css")
            )
            for method in route.methods or {"GET"}:
                candidates.add((method, path))

    allowed = set()
    for method, path in sorted(candidates):
        is_allowed = filing_route_allowed(method, path, record, path.encode())
        if is_allowed:
            allowed.add((method, path))
            continue
        status, _headers, body = server.request(method, path, token=filing)
        assert status == 403, (method, path, body)
        if method != "HEAD":
            assert body["error"]["code"] == "TOKEN_RESTRICTED", (method, path, body)
    assert allowed == {
        ("POST", f"/v1/projects/{SLUG}/ops/issue.file"),
        ("PUT", f"/v1/projects/{SLUG}/issues/media/staging/{'a' * 64}"),
    }

    issue_path = f"/v1/projects/{SLUG}/ops/issue.file"
    stage_path = f"/v1/projects/{SLUG}/issues/media/staging/{'a' * 64}"
    variants = (
        issue_path + "/",
        issue_path.replace("/ops/", "//ops/"),
        issue_path.replace("/ops/", "/%2Fops/"),
        issue_path.replace(".file", "%2Efile"),
        stage_path + "/",
        stage_path.replace("/staging/", "//staging/"),
        stage_path.replace("/staging/", "/%2Fstaging/"),
        stage_path.replace("a" * 64, "%2E" + "a" * 63),
    )
    for path in variants:
        assert not filing_route_allowed("POST", path, record, path.encode())
        assert not filing_route_allowed("PUT", path, record, path.encode())
    assert not filing_route_allowed(
        "POST", issue_path, record, issue_path.replace(".", "%2E").encode()
    )
    for method in ("OPTIONS", "DELETE", "PATCH", "GET", "HEAD"):
        for path in (issue_path, stage_path):
            with pytest.raises(OpError) as exc:
                require_filing_route(method, path, record, path.encode())
            assert exc.value.code == "TOKEN_RESTRICTED"

    def expect_http_restricted(method: str, path: str, body: bytes | None = None) -> None:
        response = WebClient(server).request(
            method,
            path,
            body=body,
            headers={"Authorization": f"Bearer {filing}"},
        )
        assert response.status == 403, (method, path, response.text)
        if method != "HEAD":
            assert response.json["error"]["code"] == "TOKEN_RESTRICTED"

    for path in variants[:4]:
        expect_http_restricted("POST", path, b"{}")
    for path in variants[4:]:
        expect_http_restricted("PUT", path, b"stage-bytes")
    for method in ("OPTIONS", "DELETE", "PATCH", "GET", "HEAD"):
        for path in (issue_path, stage_path):
            expect_http_restricted(method, path)
    expect_http_restricted("GET", "/v1/future/route")

    wrong_project, _, body = server.op(
        "beta",
        "issue.file",
        {"title": "wrong project", "source": SOURCE},
        token=filing,
        actor=SERVICE_ACTOR,
    )
    assert wrong_project == 403 and body["error"]["code"] == "FORBIDDEN"


def test_filing_credentials_cannot_mint_or_use_dashboard_sessions(
    server: ServerHandle, root: Path
) -> None:
    filing = filing_token(root)
    login = WebClient(server).login(filing)
    assert login.status == 403
    assert login.json["error"]["code"] == "TOKEN_RESTRICTED"
    sessions = root / "web_sessions.json"
    assert not sessions.exists() or json.loads(sessions.read_text())["sessions"] == []
    with pytest.raises(OpError) as exc:
        server.state.sessions.create(tokens._read(root)[0])
    assert exc.value.code == "TOKEN_RESTRICTED"


def test_existing_session_backed_by_filing_token_fails_closed(
    server: ServerHandle, root: Path
) -> None:
    filing = filing_token(root)
    token_id = tokens.parse_token(filing)[0]
    cookie = "A" * 43
    created = datetime.now(UTC).replace(microsecond=0)
    expires = created + timedelta(days=1)
    (root / "web_sessions.json").write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "sha256": hash_secret(cookie),
                        "token_id": token_id,
                        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                ]
            }
        )
    )
    web = WebClient(server)
    web.cookies[COOKIE_NAME] = cookie
    response = web.get(f"/p/{SLUG}/api/issues")
    assert response.status == 403
    assert response.json["error"]["code"] == "TOKEN_RESTRICTED"


def test_session_auth_refuses_a_filing_token_after_route_reassertion(
    server: ServerHandle, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filing = filing_token(root)
    token_id = tokens.parse_token(filing)[0]
    cookie = "B" * 43
    created = datetime.now(UTC).replace(microsecond=0)
    expires = created + timedelta(days=1)
    (root / "web_sessions.json").write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "sha256": hash_secret(cookie),
                        "token_id": token_id,
                        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                ]
            }
        )
    )
    # This isolates session_auth's own filing-only refusal from the route guard.
    monkeypatch.setattr(web_module, "require_filing_route", lambda *_args: None)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/p/{SLUG}/api/issues",
            "raw_path": f"/p/{SLUG}/api/issues".encode(),
            "headers": [(b"cookie", f"{COOKIE_NAME}={cookie}".encode())],
            "state": {"log": {}},
        }
    )
    with pytest.raises(OpError) as exc:
        web_module.session_auth(request, server.state)
    assert exc.value.code == "TOKEN_RESTRICTED"


def test_filing_token_authenticates_once_and_uses_token_operation_rate(
    server: ServerHandle, root: Path
) -> None:
    filing = filing_token(root, ops_per_minute=1)
    original = server.state.tokens.authenticate
    calls = []

    def counted(authorization: str | None):
        calls.append(authorization)
        return original(authorization)

    server.state.tokens.authenticate = counted
    first, _, _ = file_issue(server, filing, source_ref="rate-first")
    assert first == 200 and len(calls) == 1
    status, headers, body = file_issue(server, filing, source_ref="rate-second")
    assert status == 429 and body["error"]["code"] == "RATE_LIMITED"
    assert headers.get("retry-after")
    assert len(calls) == 2


def test_filing_token_byte_rate_override_is_enforced_over_http(
    root: Path,
) -> None:
    with running_server(
        root, config={"limits": {"max_issue_media_file_bytes": 1024}}
    ) as small_server:
        filing = filing_token(root, bytes_per_minute=2048)
        status, headers, body = small_server.request(
            "POST",
            f"/v1/projects/{SLUG}/ops/issue.file",
            token=filing,
            body={
                "actor": SERVICE_ACTOR,
                "params": {
                    "title": "too large",
                    "source": SOURCE,
                    "description": "x" * 4096,
                },
            },
        )
        assert status == 413 and body["error"]["code"] == "PAYLOAD_TOO_LARGE"
        assert body["error"]["details"] == {"limit_bytes": 2048, "scope": "token"}
        assert "retry-after" not in headers
        assert not (root / "projects" / SLUG / ".lattice" / "issues").exists()


def test_mint_rejects_byte_rate_below_live_media_cap_for_any_token(root: Path) -> None:
    media_cap = load_config(root).limits.max_issue_media_file_bytes
    with pytest.raises(OpError, match="max_issue_media_file_bytes"):
        filing_token(root, bytes_per_minute=media_cap - 1)
    with pytest.raises(OpError, match="max_issue_media_file_bytes"):
        tokens.create_token(
            root,
            user="human:trusted",
            machine="trusted-service",
            all_projects=True,
            bytes_per_minute=media_cap - 1,
        )


def test_staging_body_above_token_capacity_is_token_scoped_413(root: Path) -> None:
    with running_server(root, config={"limits": {"max_issue_media_file_bytes": 512}}):
        filing = filing_token(root, bytes_per_minute=512)

    # A restarted server can raise the file cap while an older token keeps its
    # explicit byte capacity. The token check must run before bucket charging.
    with running_server(
        root, config={"limits": {"max_issue_media_file_bytes": 1024}}
    ) as updated_server:
        media = jpeg() + b"x" * (600 - len(jpeg()))
        status, _, body = stage(updated_server, filing, media)
        assert status == 413 and body["error"]["code"] == "PAYLOAD_TOO_LARGE"
        assert body["error"]["details"] == {"limit_bytes": 512, "scope": "token"}


def test_default_filing_token_can_stage_a_70_mib_file(server: ServerHandle, root: Path) -> None:
    filing = filing_token(root)
    media = png() + bytes(70 * 1024 * 1024 - len(png()))
    status, _, body = stage(server, filing, media)
    assert status == 201, body
    assert body["data"]["size_bytes"] == 70 * 1024 * 1024


def test_filing_token_reuses_cached_auth_record_and_rejects_session_selection(
    server: ServerHandle, root: Path
) -> None:
    filing = filing_token(root)
    calls = []
    original = server.state.tokens.authenticate

    def counted(authorization: str | None):
        calls.append(authorization)
        return original(authorization)

    server.state.tokens.authenticate = counted
    status, _, body = server.op(
        SLUG,
        "issue.file",
        {"title": "x", "source": SOURCE, "source_ref": "auth-once"},
        token=filing,
        actor=SERVICE_ACTOR,
    )
    assert status == 200 and len(calls) == 1
    assert body["data"]["result"]["value"]["external"] is True

    status, _, refusal = server.request(
        "POST",
        f"/v1/projects/{SLUG}/ops/issue.file",
        token=filing,
        body={"params": {"title": "x", "source": SOURCE}, "actor_name": "someone"},
    )
    assert status == 403 and refusal["error"]["code"] == "TOKEN_RESTRICTED"
