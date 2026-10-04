"""LAT-368 repair 1: hosted issue media on a bound checkout, client side.

- G-8 on the media write path: with the server stopped, ``issue file
  --evidence`` and ``issue attach`` say so in plain words, with progress, the
  OS error only in ``--json`` details, and the offline window honoured; a
  dropped upload answer is retried (review B1, C13).
- A filing whose staged objects the server lost (a crash rolled back) uploads
  them again and retries once (C6).
- Offline issue reads serve the cache with one notice and say "server
  unreachable", not "not on the server" (B2, B8); ``--paths`` names a gap by
  number and name (C9).
- HEIC conversion and video transcodes run with the cache read lock released
  (B3, SPEC §9.4).
- An old server's ``ISSUES_DISABLED`` says to upgrade the server (B15).
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from lattice.core.errors import OpError
from lattice.remote import client, http, session
from lattice.server import admin
from tests.issue_media_helpers import fake_sips, heic, png, png_with_large_idat
from tests.test_remote import hosted as _hosted
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli

ACTOR = ("--actor", "agent:tester")
hosted_env = _hosted.hosted_env
WINDOW = Path(".lattice/cache/unreachable_until")


def _notices(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("lattice: ")]


@pytest.fixture()
def repo(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    admin.set_project_config(hosted_env.server_root, "demo", {"issues.enabled": True})
    repo = make_repo(tmp_path / "work" / "repo")
    result = run_cli(repo, "remote", "attach", "team", "demo")
    assert result.exit_code == 0, result.output
    return repo


@pytest.fixture()
def shot(tmp_path: Path) -> Path:
    path = tmp_path / "shot.png"
    path.write_bytes(png(8, 6))
    return path


def ok(repo: Path, *args: str, write: bool = False) -> dict:
    result = run_cli(repo, *args, "--json", *(ACTOR if write else ()))
    assert result.exit_code == 0, (args, result.output)
    return json.loads(result.stdout)["data"]


# ---------------------------------------------------------------------------
# Item 2: G-8 on the media write path
# ---------------------------------------------------------------------------


def test_filing_with_media_while_stopped_says_so_plainly(
    hosted_env: HostedEnv, repo: Path, shot: Path
) -> None:
    with hosted_env.stopped():
        url = hosted_env.url
        plain = run_cli(repo, "issue", "file", "Offline", "--evidence", str(shot), *ACTOR)
    assert plain.exit_code == 1
    assert plain.stdout == ""
    # (The read phase's catch-up notice comes first, as for every issue write.)
    assert f"lattice: server team ({url}) is not available; retrying for up to 1 s" in (
        _notices(plain.stderr)
    )
    assert plain.stderr.splitlines()[-1] == (
        f"Error: server team ({url}) is not available. Nothing was written; "
        "run the command again when it is back."
    )
    assert "errno" not in plain.stderr.lower() and "refused" not in plain.stderr.lower()
    assert (repo / WINDOW).exists()

    (repo / WINDOW).unlink()
    with hosted_env.stopped():
        url = hosted_env.url
        as_json = run_cli(repo, "issue", "file", "Offline", "--evidence", str(shot), "--json")
    error = json.loads(as_json.stdout)["error"]
    assert error["code"] == "SERVER_UNREACHABLE"
    assert set(error["details"]) == {"remote", "url", "os_error", "waited_seconds"}
    assert error["details"]["url"] == url
    assert "refused" in error["details"]["os_error"].lower()
    assert "errno" not in error["message"].lower()


def test_attach_inside_the_offline_window_fails_at_once(
    hosted_env: HostedEnv, repo: Path, shot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = ok(repo, "issue", "file", "Has no media yet", write=True)["short_id"]
    hosted_env.write_remote(retry_seconds=5)
    attempts: list[str] = []
    send = http.request

    def counting(remote: http.Remote, method: str, path: str, **kwargs: Any) -> Any:
        attempts.append(f"{method} {path}")
        return send(remote, method, path, **kwargs)

    with hosted_env.stopped():
        (repo / WINDOW).write_text(f"{time.time() + 15:.3f}\n")
        monkeypatch.setattr(http, "request", counting)
        started = time.monotonic()
        result = run_cli(repo, "issue", "attach", issue, str(shot), "--json", *ACTOR)
    assert time.monotonic() - started < 3
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "SERVER_UNREACHABLE" and error["details"]["waited_seconds"] == 0
    assert [a for a in attempts if "/staging/" in a] and len(attempts) == 1
    assert "retrying" not in result.stderr


def test_a_dropped_upload_answer_is_retried_and_the_filing_lands(
    hosted_env: HostedEnv, repo: Path, shot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted_env.write_remote(retry_seconds=3)
    uploads: list[str] = []
    send = http.request

    def drop_first_upload(remote: http.Remote, method: str, path: str, **kwargs: Any) -> Any:
        if "/staging/" in path:
            uploads.append(path)
            answer = send(remote, method, path, **kwargs)  # the server staged it
            if len(uploads) == 1:
                raise http.Unreachable("Remote end closed connection without response", sent=True)
            return answer
        return send(remote, method, path, **kwargs)

    monkeypatch.setattr(http, "request", drop_first_upload)
    result = run_cli(repo, "issue", "file", "Dropped once", "--evidence", str(shot), *ACTOR)
    assert result.exit_code == 0, result.output
    assert len(uploads) == 2 and uploads[0] == uploads[1]  # the same staged object
    assert "retrying for up to 3 s" in result.stderr
    assert "Filed DEM-I1" in result.stdout and "(1 photo)" in result.stdout


# ---------------------------------------------------------------------------
# Minor: staged objects lost to a server crash are uploaded again once
# ---------------------------------------------------------------------------


def test_a_filing_whose_staged_media_was_lost_uploads_again_once(
    hosted_env: HostedEnv, repo: Path, shot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads: list[str] = []
    posts: list[str] = []
    send = http.request
    post = client.post_operation

    def counting(remote: http.Remote, method: str, path: str, **kwargs: Any) -> Any:
        if "/staging/" in path:
            uploads.append(path)
        return send(remote, method, path, **kwargs)

    def lost_once(remote: http.Remote, project: str, op_name: str, body: dict, **kw: Any) -> Any:
        posts.append(body["op_id"])
        if len(posts) == 1:  # as after a crash that rolled the filing back
            sha = body["params"]["media"][0]["payload"]["sha256"]
            raise OpError("NOT_FOUND", f"staged media object {sha} not found for this project.")
        return post(remote, project, op_name, body, **kw)

    monkeypatch.setattr(http, "request", counting)
    monkeypatch.setattr(client, "post_operation", lost_once)
    result = run_cli(repo, "issue", "file", "Crash", "--evidence", str(shot), "--json", *ACTOR)
    assert result.exit_code == 0, result.output
    assert len(uploads) == 2 and len(posts) == 2 and posts[0] == posts[1]
    assert "no longer holds the uploaded media; uploading it again" in result.stderr
    assert [m["n"] for m in json.loads(result.stdout)["data"]["media"]] == [1]


# ---------------------------------------------------------------------------
# Item 6: offline reads
# ---------------------------------------------------------------------------


@pytest.fixture()
def with_media(repo: Path, shot: Path) -> str:
    return ok(repo, "issue", "file", "Has a photo", "--evidence", str(shot), write=True)[
        "short_id"
    ]


def test_offline_reads_say_server_unreachable_with_one_notice(
    hosted_env: HostedEnv, repo: Path, with_media: str
) -> None:
    with hosted_env.stopped():
        shown = run_cli(repo, "issue", "show", with_media, "--json")
        plain = run_cli(repo, "issue", "show", with_media)
        listed = run_cli(repo, "issue", "list")
        media = run_cli(repo, "issue", "media", with_media)
        paths = run_cli(repo, "issue", "media", with_media, "--paths")
    for result in (shown, plain, listed, media, paths):
        assert result.exit_code == 0, result.output
        notices = _notices(result.stderr)
        assert len(notices) == 1, result.stderr
        assert notices[0].startswith("lattice: cannot reach team; showing cache as of ")
    entry = json.loads(shown.stdout)["data"]["media"][0]
    assert (entry["available"], entry["missing"], entry["path"]) == ("unreachable", False, None)
    assert "server unreachable" in plain.stdout and "not on the server" not in plain.stdout
    assert "server unreachable" in media.stdout and "not on the server" not in media.stdout
    assert paths.stdout == ""
    assert "not fetched: media 1 (shot.png): server unreachable" in paths.stderr
    assert "None" not in paths.stderr


def test_reads_inside_the_window_never_ask_for_availability(
    hosted_env: HostedEnv, repo: Path, with_media: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    send = http.request

    def counting(remote: http.Remote, method: str, path: str, **kwargs: Any) -> Any:
        asked.append(path)
        return send(remote, method, path, **kwargs)

    monkeypatch.setattr(http, "request", counting)
    (repo / WINDOW).write_text(f"{time.time() + 15:.3f}\n")
    result = run_cli(repo, "issue", "show", with_media, "--json")
    assert result.exit_code == 0, result.output
    assert not [p for p in asked if "availability" in p]
    assert json.loads(result.stdout)["data"]["media"][0]["available"] == "unreachable"


def test_paths_names_a_missing_item_by_number_and_name(
    hosted_env: HostedEnv, repo: Path, with_media: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.remote import issue_media

    monkeypatch.setattr(
        issue_media, "availability", lambda _r, _p, ids: {issue_id: [] for issue_id in ids}
    )
    result = run_cli(repo, "issue", "media", with_media, "--paths")
    assert result.exit_code == 0, result.output
    assert "missing: media 1 (shot.png): not on the server" in result.stderr
    assert "missing: None" not in result.stderr


# ---------------------------------------------------------------------------
# Item 7: conversions run with the cache read lock released
# ---------------------------------------------------------------------------


def _lock_held(repo: Path) -> bool:
    if session._locks:
        return True
    fd = os.open(repo / ".lattice" / "locks" / "cache_rw.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@pytest.mark.parametrize("command", ["file", "attach"])
def test_conversion_runs_with_the_read_lock_released(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from lattice.integrations import ffmpeg

    photo = tmp_path / "IMG_1.HEIC"
    photo.write_bytes(heic())
    monkeypatch.setenv("LATTICE_SIPS", str(fake_sips(tmp_path / "sips")))
    issue = ok(repo, "issue", "file", "Target", write=True)["short_id"]
    held: list[bool] = []
    run = ffmpeg._run

    def recording(argv: list[str], timeout: float) -> Any:
        held.append(_lock_held(repo))
        return run(argv, timeout)

    monkeypatch.setattr(ffmpeg, "_run", recording)
    args = (
        ("issue", "file", "Converted", "--evidence", str(photo))
        if command == "file"
        else ("issue", "attach", issue, str(photo))
    )
    result = run_cli(repo, *args, "--json", *ACTOR)
    assert result.exit_code == 0, result.output
    assert held == [False]
    media = json.loads(result.stdout)["data"]["media"]
    assert media[-1]["content_type"] == "image/jpeg"


# ---------------------------------------------------------------------------
# Minor B15: a new client on an old server
# ---------------------------------------------------------------------------


def test_an_old_server_is_named_instead_of_a_command_it_refuses(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "old" / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    state = session._state
    monkeypatch.setattr(session, "_state", lambda h: {**state(h), "server_version": "0.2.1"})
    result = run_cli(repo, "issue", "list", "--json")
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "ISSUES_DISABLED"
    assert "runs Lattice 0.2.1" in error["message"] and "Upgrade the server" in error["message"]
    assert "--set issues.enabled=true" not in error["message"]


# ---------------------------------------------------------------------------
# Round 2: media against an old server, and a quota refusal's details
# ---------------------------------------------------------------------------


def test_media_against_a_server_without_the_upload_route_says_to_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_route(*_args: Any, **_kwargs: Any) -> Any:
        raise http.ServerError(
            "NOT_FOUND",
            "no route PUT /v1/projects/demo/issues/media/staging/abc",
            {},
            status=404,
        )

    monkeypatch.setattr(client.http, "request", no_route)
    remote = http.Remote(alias="team", url="http://127.0.0.1:1", token="t", retry_seconds=1.0)
    with pytest.raises(OpError) as caught:
        client._upload(remote, "/v1/projects/demo/issues/media/staging/abc", b"x", offline=False)
    assert "upgrade the server to 0.2.2" in caught.value.message
    assert "no route" not in caught.value.message


def test_a_media_quota_refusal_keeps_its_limit_and_used_bytes_in_json(
    hosted_env: HostedEnv, repo: Path, tmp_path: Path
) -> None:
    config = json.loads((hosted_env.server_root / "server.json").read_text())
    config.setdefault("limits", {})["max_issue_media_project_bytes"] = 1000
    (hosted_env.server_root / "server.json").write_text(json.dumps(config))
    hosted_env.stop()
    hosted_env.start()
    photo = tmp_path / "big.png"
    photo.write_bytes(png_with_large_idat(4000))
    result = run_cli(repo, "issue", "file", "too big", "--evidence", str(photo), *ACTOR, "--json")
    assert result.exit_code == 1, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "MEDIA_QUOTA_EXCEEDED"
    assert error["details"]["limit_bytes"] == 1000 and "used_bytes" in error["details"]
