"""AC-24 (hosted, API level) and AC-16 (the per-project dashboard): the hosted
dashboard at ``/p/<slug>/`` over the authoritative board (SPEC §10)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest

from lattice.dashboard import api
from lattice.dashboard.server import STATIC_DIR
from lattice.server.testing import ServerHandle, running_server, wait_for
from tests.test_server.conftest import board_hash, create_task, mint
from tests.test_server.web_client import WebClient

CSP_FIXED = (
    "default-src 'self'; script-src 'self'{hashes}; style-src 'self' 'unsafe-inline'; "
    "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)

READ_PATHS = [
    "/api/config",
    "/api/tasks",
    "/api/stats",
    "/api/activity",
    "/api/archived",
    "/api/graph",
]

HOSTILE = 'it\'s "quoted" <script>alert(1)</script> \x1b[31mred\x1b[0m \x07'


def _expected_csp() -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    hashes = []
    for match in re.finditer(r"<script>(.*?)</script>", html, re.S):
        digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
        hashes.append(f" 'sha256-{base64.b64encode(digest).decode()}'")
    assert hashes, "index.html has an inline script block"
    return CSP_FIXED.format(hashes="".join(hashes))


def _logged_in(server: ServerHandle, token: str) -> WebClient:
    web = WebClient(server)
    response = web.login(token)
    assert response.status == 303, response.text
    assert web.session
    return web


def _assert_headers(response, where: str) -> None:
    assert response.headers.get("x-content-type-options") == "nosniff", where
    assert response.headers.get("content-security-policy") == _expected_csp(), where


@pytest.fixture()
def web(server: ServerHandle, root: Path) -> WebClient:
    return _logged_in(server, mint(root, projects=["alpha"]))


# ---------------------------------------------------------------------------
# AC-16: the page and its assets
# ---------------------------------------------------------------------------


class TestPage:
    def test_without_credential_redirects_to_login(self, server: ServerHandle) -> None:
        anon = WebClient(server)
        response = anon.get("/p/alpha/")
        assert response.status == 303
        assert response.headers["location"] == "/login?next=/p/alpha/"
        assert anon.get("/p/alpha/api/tasks").status == 401
        assert anon.get("/p/alpha/static/escape.js").status == 401

    def test_serves_the_dashboard_and_its_assets(self, web: WebClient) -> None:
        page = web.get("/p/alpha/")
        assert page.status == 200
        assert page.headers["content-type"].startswith("text/html")
        assert page.text == (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = web.get("/p/alpha/static/escape.js")
        assert script.status == 200
        assert script.headers["content-type"].startswith("application/javascript")
        issue_script = web.get("/p/alpha/static/issue-view.js")
        issue_style = web.get("/p/alpha/static/issue-view.css")
        assert issue_script.status == 200 and issue_script.headers["content-type"].startswith(
            "application/javascript"
        )
        assert issue_style.status == 200 and issue_style.headers["content-type"].startswith(
            "text/css"
        )
        assert web.get("/p/alpha/favicon.ico").status == 200
        assert web.get("/p/alpha/static/../server.py").status in (403, 404)
        for src in re.findall(r'src="(static/vendor/[^"]+)"', page.text):
            vendored = web.get(f"/p/alpha/{src}")  # same origin: the CSP allows it
            assert vendored.status == 200, src
            _assert_headers(vendored, src)

    def test_bare_slug_redirects_to_its_base_path(self, web: WebClient) -> None:
        response = web.get("/p/alpha")
        assert response.status in (301, 307, 308)
        assert response.headers["location"] == "/p/alpha/"

    def test_issue_reads_say_not_available_on_any_host(self, web: WebClient) -> None:
        """Issues are not on hosted boards yet (LAT-368): reads answer what writes do, on
        any Host; the session credential is the gate, as for every other route."""
        for host in ("evil.example", "atlas.tailnet:8443"):
            for path in ("/p/alpha/api/issues", "/p/alpha/api/issues/ALP-I1"):
                response = web.get(path, Host=host)
                assert response.status == 400, (host, path)
                assert response.json["error"]["code"] == "LOCAL_ONLY"
        anonymous = WebClient(web.server)
        assert anonymous.get("/p/alpha/api/issues").status == 401

    def test_other_project_is_403_and_missing_is_404(self, server: ServerHandle, root) -> None:
        web = _logged_in(server, mint(root, projects=["alpha"]))
        assert web.get("/p/beta/").status == 403
        assert web.get("/p/beta/api/tasks").status == 403
        everywhere = _logged_in(server, mint(root))
        assert everywhere.get("/p/nope/api/tasks").status == 404

    def test_bearer_token_reads_the_api(self, server: ServerHandle, root: Path) -> None:
        token = mint(root, projects=["alpha"])
        status, _, body = server.request("GET", "/p/alpha/api/tasks", token=token)
        assert status == 200 and body["ok"] is True


# ---------------------------------------------------------------------------
# AC-24: reads
# ---------------------------------------------------------------------------


class TestReads:
    def test_gets_match_the_local_dashboard(self, server, root, web: WebClient) -> None:
        token = mint(root)
        task = create_task(server, token, title="first")
        server.op("alpha", "task.comment", {"task": task["id"], "text": "hi"}, token=token)
        board = root / "projects" / "alpha" / ".lattice"
        paths = [
            *READ_PATHS,
            f"/api/tasks/{task['id']}",
            f"/api/tasks/{task['id']}/events",
            f"/api/tasks/{task['id']}/comments",
            f"/api/tasks/{task['id']}/full",
        ]
        # Three heads: fresh, after a comment, after an archive. Later heads reuse
        # cached replays of unchanged tasks, so any mutation of one shows here.
        other = create_task(server, token, title="second")
        for step in range(3):
            if step == 1:
                server.op("alpha", "task.comment", {"task": task["id"], "text": "2"}, token=token)
            if step == 2:
                server.op("alpha", "task.archive", {"task": other["id"]}, token=token)
            for path in paths:
                hosted = web.get("/p/alpha" + path)
                local = api.route_get(board, path)
                assert hosted.status == local.status, (step, path)
                assert hosted.json == json.loads(local.body()), (step, path)
                assert hosted.headers["cache-control"] == "no-store", path

    def test_git_views_are_unavailable_when_hosted(self, web: WebClient) -> None:
        body = web.get("/p/alpha/api/git").json
        assert body == {"ok": True, "data": {"available": False, "reason": "hosted"}}

    def test_two_viewers_at_one_seq_cost_one_computation(self, server, root, monkeypatch) -> None:
        calls: list[str] = []
        original = api.route_get

        def counting(ld, path, *args, **kwargs):
            calls.append(path)
            return original(ld, path, *args, **kwargs)

        monkeypatch.setattr(api, "route_get", counting)
        token = mint(root, projects=["alpha"])
        viewers = [_logged_in(server, token), _logged_in(server, token)]
        for viewer in viewers:
            for path in READ_PATHS:
                assert viewer.get("/p/alpha" + path).status == 200
        assert sorted(calls) == sorted(READ_PATHS)

        create_task(server, token)  # a new head: every endpoint computes once more
        calls.clear()
        for viewer in viewers:
            assert viewer.get("/p/alpha/api/tasks").status == 200
        assert calls == ["/api/tasks"]

    def test_etag_answers_304_from_the_memo(self, web: WebClient) -> None:
        first = web.get("/p/alpha/api/graph")
        etag = first.headers["etag"]
        again = web.request("GET", "/p/alpha/api/graph", headers={"If-None-Match": etag})
        assert again.status == 304


# ---------------------------------------------------------------------------
# AC-24: writes
# ---------------------------------------------------------------------------


def _drag(web: WebClient, task_id: str, status: str, **kw):
    return web.post_json(f"/p/alpha/api/tasks/{task_id}/status", {"status": status}, **kw)


class TestWrites:
    def test_plan_gate_refuses_a_drag(self, web: WebClient) -> None:
        created = web.post_json("/p/alpha/api/tasks", {"title": "gated"})
        assert created.status == 201, created.text
        task_id = created.json["data"]["id"]
        for step in ("in_planning", "planned"):
            assert _drag(web, task_id, step).status == 200
        refused = _drag(web, task_id, "in_progress")
        assert refused.status == 422
        assert refused.json["error"]["code"] == "PLAN_REQUIRED"
        short_id = created.json["data"]["short_id"]
        escape = f'lattice status {short_id} in_progress --force --reason "..."'
        assert escape in refused.json["error"]["message"]

    def test_foreign_origin_is_refused(self, server, root, web: WebClient) -> None:
        before = board_hash(root, "alpha")
        for origin in ("http://evil.example", "null", None):
            response = web.post_json("/p/alpha/api/tasks", {"title": "x"}, origin=origin)
            assert response.status == 403, origin
            assert response.json["error"]["code"] == "FORBIDDEN"
        assert board_hash(root, "alpha") == before

    def test_public_origins_are_accepted(self, root: Path) -> None:
        proxy = "https://lattice.example.internal"
        with running_server(root, config={"public_origins": [proxy]}) as server:
            web = _logged_in(server, mint(root, projects=["alpha"]))
            ok = web.post_json("/p/alpha/api/tasks", {"title": "via proxy"}, origin=proxy)
            assert ok.status == 201, ok.text
            other = web.post_json(
                "/p/alpha/api/tasks", {"title": "x"}, origin="https://other.example.internal"
            )
            assert other.status == 403

    def test_json_content_type_is_required(self, web: WebClient) -> None:
        response = web.request(
            "POST",
            "/p/alpha/api/tasks",
            body=b"title=x",
            headers={"Origin": web.origin, "Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status == 415

    @pytest.mark.parametrize(
        "actors", [("human:alice",), ("human:alice", "agent:*")], ids=["strict", "person"]
    )
    def test_writes_as_the_browser_actor(self, server, root, actors) -> None:
        token = mint(root, projects=["alpha"], actors=actors)
        web = _logged_in(server, token)
        created = web.post_json(
            "/p/alpha/api/tasks", {"title": "from browser", "actor": "agent:mallory"}
        )
        assert created.status == 201, created.text
        task_id = created.json["data"]["id"]
        commented = web.post_json(
            f"/p/alpha/api/tasks/{task_id}/comment",
            {"body": "hello", "actor": "human:mallory"},
        )
        assert commented.status == 200, commented.text
        events = web.get(f"/p/alpha/api/tasks/{task_id}/events").json["data"]
        assert {e["actor"] for e in events} == {"human:alice"}
        for event in events:
            assert event["origin"]["reported"] == {"source": "browser"}
            assert event["origin"]["authenticated"]["user"] == "human:alice"

    def test_token_without_browser_actor_is_missing_actor(self, server, root) -> None:
        token = mint(root, projects=["alpha"], actors=("agent:*",))
        web = _logged_in(server, token)
        refused = web.post_json("/p/alpha/api/tasks", {"title": "x"})
        assert refused.json["error"]["code"] == "MISSING_ACTOR"

    def test_open_prose_is_local_only(self, server, root, web: WebClient) -> None:
        task = create_task(server, mint(root))
        for sub in ("open-notes", "open-plans"):
            response = web.post_json(f"/p/alpha/api/tasks/{task['id']}/{sub}", {})
            assert response.json["error"]["code"] == "LOCAL_ONLY", sub

    def test_session_cookie_is_refused_on_v1(self, server, root, web: WebClient) -> None:
        before = board_hash(root, "alpha")
        response = web.post_json("/v1/projects/alpha/ops/task.create", {"params": {"title": "x"}})
        assert response.status == 401
        for path in (
            "/v1/info",
            "/v1/projects",
            "/v1/projects/alpha/sync?since=0",
            "/v1/projects/alpha/files/config.json",
            "/v1/projects/alpha/tasks",
        ):
            assert web.get(path).status == 401, path
        assert board_hash(root, "alpha") == before


# ---------------------------------------------------------------------------
# AC-24: headers and hostile content
# ---------------------------------------------------------------------------


class TestHeaders:
    def test_every_page_and_api_response_carries_csp_and_nosniff(
        self, server, root, web: WebClient
    ) -> None:
        anon = WebClient(server)
        for response, where in [
            (anon.get("/login"), "login form"),
            (anon.get("/"), "index, anonymous"),
            (web.get("/"), "index"),
            (web.get("/p/alpha/"), "page"),
            (web.get("/p/alpha/static/escape.js"), "asset"),
            (web.get("/p/alpha/api/tasks"), "api"),
            (web.get("/p/alpha/api/nope"), "api 404"),
            (web.get("/p/beta/api/tasks"), "api 403"),
            (anon.get("/p/alpha/api/tasks"), "api 401"),
            (web.post_json("/p/alpha/api/tasks", {"title": "x"}, origin=None), "post 403"),
            (web.post_json("/p/alpha/api/tasks", {"title": "x"}), "post"),
        ]:
            _assert_headers(response, where)

    def test_hostile_content_is_served_only_as_json(self, server, root, web) -> None:
        token = mint(root)
        task = create_task(server, token, title=HOSTILE, description=HOSTILE)
        server.op("alpha", "task.comment", {"task": task["id"], "text": HOSTILE}, token=token)
        server.op(
            "alpha",
            "task.comment",
            {"task": task["id"], "text": "x"},
            token=token,
            origin={"reported": {"host": "<script>'\"", "worktree": "</script><img src=x>"}},
        )
        seen = 0
        for path in [
            *READ_PATHS,
            f"/api/tasks/{task['id']}",
            f"/api/tasks/{task['id']}/events",
            f"/api/tasks/{task['id']}/full",
        ]:
            response = web.get("/p/alpha" + path)
            assert response.headers["content-type"].startswith("application/json"), path
            _assert_headers(response, path)
            seen += "<script>" in response.json.__repr__()
        assert seen  # the hostile strings reach the page only inside JSON
        page = web.get("/p/alpha/")
        assert "alert(1)" not in page.text and "\x1b" not in page.text


# ---------------------------------------------------------------------------
# Amendment 1: every read passes authorization and admission, warm memo or not
# ---------------------------------------------------------------------------


def _admin(root: Path, *args: str):
    from click.testing import CliRunner

    from lattice.cli.main import cli

    result = CliRunner().invoke(cli, ["server", "project", *args, "--root", str(root)])
    assert result.exit_code == 0, result.output
    return result


class TestWarmMemo:
    def _warm(self, web: WebClient) -> dict:
        first = web.get("/p/alpha/api/config")
        assert first.status == 200
        return first.json

    def test_release_and_reload(self, server, root, web: WebClient) -> None:
        """A released project reloads at the next read's admission, and the read
        reflects the board as reloaded, not the warm memo. (``project unload`` and
        ``reload`` are not on v2 yet; ``release()`` is the lease they drop.)"""
        self._warm(web)
        project = server.project("alpha")
        with project.locked():
            project.release()
        config_path = root / "projects" / "alpha" / ".lattice" / "config.json"
        config = json.loads(config_path.read_text())
        config["dashboard"] = {"title": "edited while unloaded"}
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        response = web.get("/p/alpha/api/config")
        assert response.status == 200
        assert response.json["data"]["dashboard"] == {"title": "edited while unloaded"}

    def test_quarantine(self, server, root, web: WebClient) -> None:
        self._warm(web)
        project = server.project("alpha")
        with project.locked():
            project._mark_unavailable("test quarantine")
        response = web.get("/p/alpha/api/config")
        assert response.status == 503
        assert response.json["error"]["code"] == "BOARD_UNAVAILABLE"
        with project.locked():
            project.release()  # what a reload does first
        assert web.get("/p/alpha/api/config").status == 200

    def test_rotation_recomputes(self, server, root, web: WebClient, monkeypatch) -> None:
        self._warm(web)
        calls: list[str] = []
        original = api.route_get
        monkeypatch.setattr(
            api, "route_get", lambda ld, p, *a, **k: calls.append(p) or original(ld, p, *a, **k)
        )
        _admin(root, "rotate-epoch", "alpha")
        assert web.get("/p/alpha/api/config").status == 200
        assert calls == ["/api/config"]

    def test_external_modification_is_seen(self, server, root, web: WebClient) -> None:
        self._warm(web)
        config_path = root / "projects" / "alpha" / ".lattice" / "config.json"
        config = json.loads(config_path.read_text())
        config["dashboard"] = {"title": "edited by hand"}
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        seen = web.get("/p/alpha/api/config").json["data"]
        assert seen["dashboard"] == {"title": "edited by hand"}

    def test_authorization_runs_before_the_memo(self, server, root) -> None:
        from lattice.server import tokens as token_admin

        narrow = mint(root, projects=["alpha", "ghost"])
        web = _logged_in(server, narrow)
        self._warm(web)
        wide = _logged_in(server, mint(root))
        assert wide.get("/p/alpha/api/config").status == 200
        assert web.get("/p/beta/api/config").status == 403  # existing, not granted
        assert web.get("/p/other/api/config").status == 403  # absent, not granted
        assert web.get("/p/ghost/api/config").status == 404  # absent, granted
        token_admin.revoke_token(root, token_admin.parse_token(narrow)[0])
        assert web.get("/p/alpha/api/config").status == 401

    def test_invalid_authorization_never_falls_back_to_the_cookie(self, web: WebClient) -> None:
        for header in ("Bearer nope", "Basic abc", ""):
            response = web.request("GET", "/p/alpha/api/config", headers={"Authorization": header})
            assert response.status == 401, header


# ---------------------------------------------------------------------------
# Amendment 2: browser writes are retry-safe with a page-chosen op_id
# ---------------------------------------------------------------------------


class TestRetries:
    def test_a_retried_create_applies_once(self, server, root, web: WebClient) -> None:
        op_id = "op_01J9Z0000000000000000000AA"
        first = web.post_json("/p/alpha/api/tasks", {"title": "once"}, **{"Lattice-Op-Id": op_id})
        again = web.post_json("/p/alpha/api/tasks", {"title": "once"}, **{"Lattice-Op-Id": op_id})
        assert first.status == again.status == 201, again.text
        assert first.json["data"]["id"] == again.json["data"]["id"]
        titles = [t["title"] for t in web.get("/p/alpha/api/tasks").json["data"]]
        assert titles == ["once"]

    def test_a_retried_comment_applies_once(self, server, root, web: WebClient) -> None:
        task_id = web.post_json("/p/alpha/api/tasks", {"title": "t"}).json["data"]["id"]
        op_id = "op_01J9Z0000000000000000000BB"
        for _ in range(2):
            response = web.post_json(
                f"/p/alpha/api/tasks/{task_id}/comment",
                {"body": "just once"},
                **{"Lattice-Op-Id": op_id},
            )
            assert response.status == 200, response.text
        comments = web.get(f"/p/alpha/api/tasks/{task_id}/comments").json["data"]
        assert [c["body"] for c in comments] == ["just once"]

    def test_a_reused_op_id_with_other_arguments_is_refused(self, web: WebClient) -> None:
        op_id = "op_01J9Z0000000000000000000CC"
        web.post_json("/p/alpha/api/tasks", {"title": "a"}, **{"Lattice-Op-Id": op_id})
        other = web.post_json("/p/alpha/api/tasks", {"title": "b"}, **{"Lattice-Op-Id": op_id})
        assert other.status == 409
        assert other.json["error"]["details"]["reason"] == "OP_ID_REUSED"

    def test_a_malformed_op_id_is_refused(self, root, web: WebClient) -> None:
        before = board_hash(root, "alpha")
        bad = web.post_json("/p/alpha/api/tasks", {"title": "a"}, **{"Lattice-Op-Id": "op_../x"})
        assert bad.status == 400
        assert board_hash(root, "alpha") == before

    def test_without_an_op_id_each_post_applies(self, web: WebClient) -> None:
        for _ in range(2):
            assert web.post_json("/p/alpha/api/tasks", {"title": "twice"}).status == 201
        assert len(web.get("/p/alpha/api/tasks").json["data"]) == 2


# ---------------------------------------------------------------------------
# Amendment 5: one header policy, on every status
# ---------------------------------------------------------------------------


class TestHeaderPolicy:
    def test_every_status_carries_the_policy(self, server, root, web, monkeypatch) -> None:
        anon = WebClient(server)
        responses = {
            "200 api": web.get("/p/alpha/api/tasks"),
            "308 bare slug": web.get("/p/alpha"),
            "303 page": anon.get("/p/alpha/"),
            "401 api": anon.get("/p/alpha/api/tasks"),
            "403 api": web.get("/p/beta/api/tasks"),
            "404 api": web.get("/p/alpha/api/nope"),
        }

        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(api, "route_get", boom)
        responses["500 api"] = web.get("/p/alpha/api/stats?fresh=1")
        assert responses["500 api"].status == 500
        for where, response in responses.items():
            assert str(response.status) == where.split()[0], where
            _assert_headers(response, where)
            if "api" in where:
                assert response.headers.get("cache-control") == "no-store", where

    def test_v1_keeps_its_own_headers(self, server, root) -> None:
        status, headers, _ = server.request("GET", "/v1/info", token=mint(root))
        assert status == 200
        assert headers["cache-control"] == "no-store"
        assert "content-security-policy" not in headers


# ---------------------------------------------------------------------------
# The session cookie never reaches /v1, except the stream
# ---------------------------------------------------------------------------


class TestCookieBoundary:
    def test_full_v1_matrix(self, server, root, web: WebClient) -> None:
        task = create_task(server, mint(root))
        for path in (
            "/v1/projects/alpha/ops/op_01J9Z0000000000000000000DD",
            f"/v1/projects/alpha/tasks/{task['id']}",
            "/v1/projects/alpha/tasks",
            "/v1/nope",
            "/v1/projects/alpha/stream/nope",
        ):
            assert web.get(path).status == 401, path

    def test_session_stream_boundary(self, server, root, web: WebClient) -> None:
        from lattice.server.testing import open_stream

        cookie = {"Cookie": f"lattice_session={web.session}"}
        ok = open_stream(server.url, "alpha", None, headers=cookie)
        assert ok.status == 200
        ok.close()
        same = open_stream(server.url, "alpha", None, headers={**cookie, "Origin": server.url})
        assert same.status == 200
        same.close()
        foreign = open_stream(
            server.url, "alpha", None, headers={**cookie, "Origin": "http://evil.example"}
        )
        assert foreign.status == 403
        foreign.close()
        other = open_stream(server.url, "beta", None, headers=cookie)
        assert other.status == 403
        other.close()
        # A present Authorization header decides alone, even when invalid.
        bad = open_stream(server.url, "alpha", "lat_nope", headers=cookie)
        assert bad.status == 401
        bad.close()


# ---------------------------------------------------------------------------
# B2 (H-13b review): the replay cache is scoped and never trusts file stats
# ---------------------------------------------------------------------------


class TestReplayCache:
    def test_a_same_size_rewrite_is_never_served_stale(self, server, root, web) -> None:
        import os

        token = mint(root)
        task = create_task(server, token, title="aaaa")
        other = create_task(server, token, title="other")
        titles = {t["id"]: t["title"] for t in web.get("/p/alpha/api/tasks").json["data"]}
        assert titles[task["id"]] == "aaaa"
        path = root / "projects" / "alpha" / ".lattice" / "events" / f"{task['id']}.jsonl"
        st = path.stat()
        data = path.read_bytes()
        with open(path, "r+b") as handle:
            handle.write(data.replace(b'"aaaa"', b'"bbbb"'))
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert (path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ino) == (
            st.st_size,
            st.st_mtime_ns,
            st.st_ino,
        )
        # The head moves through a real write and admission; the rewritten log
        # is replayed again, never served from the cache.
        server.op("alpha", "task.comment", {"task": other["id"], "text": "x"}, token=token)
        titles = {t["id"]: t["title"] for t in web.get("/p/alpha/api/tasks").json["data"]}
        assert titles[task["id"]] == "bbbb"

    def test_scope_follows_head_epoch_and_load(self, server, root, web) -> None:
        token = mint(root)
        web.get("/p/alpha/api/tasks")
        memo = server.state.dashboard_memos.for_project("alpha")
        first = memo.authorities
        head = first.scope
        create_task(server, token)
        web.get("/p/alpha/api/tasks")
        assert memo.authorities is first and first.scope != head  # new head: new scope
        _admin(root, "rotate-epoch", "alpha")
        web.get("/p/alpha/api/tasks")
        assert memo.authorities is not first  # new epoch: emptied
        second = memo.authorities
        project = server.project("alpha")
        with project.locked():
            project.release()
        web.get("/p/alpha/api/tasks")
        assert memo.authorities is not second  # new load: emptied


# ---------------------------------------------------------------------------
# B4 (H-13b review): a retry after a dropped connection applies once
# ---------------------------------------------------------------------------


def _send_and_drop(server: ServerHandle, web: WebClient, path: str, data: dict, op_id: str):
    """Send a POST in full, then close the connection without reading a byte."""
    import socket

    body = json.dumps(data).encode()
    head = (
        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
        f"Origin: {web.origin}\r\nContent-Type: application/json\r\n"
        f"Cookie: lattice_session={web.session}\r\nLattice-Op-Id: {op_id}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode()
    sock = socket.create_connection(("127.0.0.1", server.port))
    sock.sendall(head + body)
    sock.close()


class TestDroppedRetries:
    def test_create(self, server, root, web: WebClient) -> None:
        op_id = "op_01J9Z0000000000000000000EE"
        _send_and_drop(server, web, "/p/alpha/api/tasks", {"title": "dropped"}, op_id)
        # The dropped request commits; its response was never read.
        assert wait_for(lambda: len(web.get("/p/alpha/api/tasks").json["data"]) == 1)
        retry = web.post_json(
            "/p/alpha/api/tasks", {"title": "dropped"}, **{"Lattice-Op-Id": op_id}
        )
        assert retry.status == 201, retry.text
        tasks = web.get("/p/alpha/api/tasks").json["data"]
        assert [t["title"] for t in tasks] == ["dropped"]
        assert retry.json["data"]["id"] == tasks[0]["id"]  # the original result

    def test_comment(self, server, root, web: WebClient) -> None:
        task_id = web.post_json("/p/alpha/api/tasks", {"title": "t"}).json["data"]["id"]
        op_id = "op_01J9Z0000000000000000000FF"
        path = f"/p/alpha/api/tasks/{task_id}/comment"
        _send_and_drop(server, web, path, {"body": "only once"}, op_id)
        comments_url = f"/p/alpha/api/tasks/{task_id}/comments"
        assert wait_for(lambda: len(web.get(comments_url).json["data"]) == 1)
        retry = web.post_json(path, {"body": "only once"}, **{"Lattice-Op-Id": op_id})
        assert retry.status == 200, retry.text
        comments = web.get(f"/p/alpha/api/tasks/{task_id}/comments").json["data"]
        assert [c["body"] for c in comments] == ["only once"]
        assert retry.json["data"]["comment_count"] == 1


# ---------------------------------------------------------------------------
# Architect ruling (PR #89): status POSTs keep force + reason, as the CLI's
# --force --reason; SPEC §10's "no force control" means no UI control.
# ---------------------------------------------------------------------------


class TestForce:
    def test_force_with_reason_is_recorded_as_the_browser_actor(self, web: WebClient) -> None:
        task_id = web.post_json("/p/alpha/api/tasks", {"title": "forced"}).json["data"]["id"]
        for step in ("in_planning", "planned"):
            assert _drag(web, task_id, step).status == 200
        bare = web.post_json(
            f"/p/alpha/api/tasks/{task_id}/status", {"status": "in_progress", "force": True}
        )
        assert bare.status == 400
        assert bare.json["error"]["code"] == "VALIDATION_ERROR"
        assert "--reason is required" in bare.json["error"]["message"]
        forced = web.post_json(
            f"/p/alpha/api/tasks/{task_id}/status",
            {"status": "in_progress", "force": True, "reason": "plan lives elsewhere"},
        )
        assert forced.status == 200, forced.text
        assert forced.json["data"]["status"] == "in_progress"
        events = web.get(f"/p/alpha/api/tasks/{task_id}/events").json["data"]
        change = next(e for e in events if e["type"] == "status_changed")  # newest first
        assert change["actor"] == "human:alice"
        assert change["data"]["to"] == "in_progress"
        assert change["data"]["force"] is True
        assert change["data"]["reason"] == "plan lives elsewhere"
