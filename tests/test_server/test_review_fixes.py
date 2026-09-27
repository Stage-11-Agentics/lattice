"""Regression tests for the pre-handoff review of H-9: control requests never act on a
board the server does not hold and never wedge a project; the commit point failing
quarantines the project; no-actor operations run as the token's default actor; hostile
request shapes answer 400."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.server import control
from lattice.server.config import ServerConfigError, parse_config
from lattice.server.testing import running_server
from lattice.storage.ownership import release_owner_flock, try_owner_flock
from tests.test_server.conftest import board_hash, create_task, mint


def _journal_lines(root: Path, slug: str = "alpha") -> list[str]:
    path = root / "projects" / slug / ".lattice" / "hosted" / "journal.jsonl"
    return path.read_text().splitlines()


def test_control_requests_for_an_unheld_project_are_answered_not_run(root: Path) -> None:
    board = root / "projects" / "alpha" / ".lattice"
    fd = try_owner_flock(board)  # another process holds alpha's lease
    try:
        with running_server(root) as server:
            assert server.project("alpha").state == "unavailable"
            config_before = (board / "config.json").read_bytes()
            answer = control.send_request(
                board, "set-config", {"set": {"review_mode": "triple"}}, wait_seconds=5
            )
            assert answer["ok"] is False and answer["error"]["code"] == "BOARD_UNAVAILABLE"
            assert (board / "config.json").read_bytes() == config_before
            assert _journal_lines(root) == []
            assert control.pending_requests(board) == []
    finally:
        release_owner_flock(fd)


def test_a_crashing_control_request_is_answered_and_the_project_keeps_serving(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(_project, _request):  # noqa: ANN001, ANN202
        raise ValueError("config.json is corrupt")

    monkeypatch.setitem(control.ACTIONS, "xtest-crash", crash)
    token = mint(root)
    board = root / "projects" / "alpha" / ".lattice"
    control_dir = board / "hosted" / "control"
    with running_server(root) as server:
        for name, request in (
            ("01J9Z0000000000000000000AB", {"action": ["not", "a", "string"]}),
            ("01J9Z0000000000000000000AC", {"action": "xtest-crash"}),
        ):
            (control_dir / f"{name}.json").write_text(json.dumps(request))
            create_task(server, token)  # admission runs the request, then the write
            answer = json.loads((control_dir / f"{name}.done").read_text())
            assert answer["ok"] is False
        assert answer["error"]["code"] == "INTERNAL_ERROR"
        assert "ValueError" in answer["error"]["message"]
        assert control.pending_requests(board) == []
        assert any(x["event"] == "control_request_crashed" for x in server.log_lines)
        create_task(server, token)


def test_a_failed_commit_point_quarantines_the_project(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lattice.server.journal as journal_module

    token = mint(root)
    with running_server(root) as server:
        create_task(server, token)
        real = journal_module.jsonl_append

        def torn(path, line):  # noqa: ANN001, ANN202
            with open(path, "a") as fh:
                fh.write(line[:10])
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(journal_module, "jsonl_append", torn)
        status, _, body = server.op("alpha", "task.create", {"title": "x"}, token=token)
        assert status == 503 and body["error"]["code"] == "BOARD_UNAVAILABLE"
        monkeypatch.setattr(journal_module, "jsonl_append", real)
        lines = _journal_lines(root)
        assert len(lines) == 1 and all(json.loads(x) for x in lines)  # no torn tail
        assert server.project("alpha").state == "unavailable"
        status, _, body = server.op("alpha", "task.create", {"title": "y"}, token=token)
        assert status == 503
        create_task(server, token, "beta")
        assert any(x["event"] == "uncommitted_writes" for x in server.log_lines)
        # B4: the quarantine is published at once, so the admin CLI sees it
        from click.testing import CliRunner

        from lattice.cli.main import cli

        listed = CliRunner().invoke(
            cli, ["server", "project", "list", "--root", str(root), "--json"]
        )
        rows = {r["slug"]: r["state"] for r in json.loads(listed.output)["data"]}
        assert rows == {"alpha": "unavailable", "beta": "loaded"}


def test_no_actor_operations_run_as_the_tokens_default_actor(root: Path) -> None:
    person = mint(root)
    wildcard = mint(root, actors=["agent:*"])
    with running_server(root) as server:
        status, _, body = server.op("alpha", "xtest.no_actor", {}, token=person)
        assert status == 200, body
        assert body["data"]["result"]["value"] == {"actor": None}
        line = json.loads(_journal_lines(root)[-1])
        assert line["op"] == "xtest.no_actor"
        status, _, body = server.op("alpha", "xtest.no_actor", {}, token=wildcard)
        assert status == 400 and body["error"]["code"] == "MISSING_ACTOR"


def test_deeply_nested_json_is_a_validation_error(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        body = b'{"params": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
        status, _, reply = server.request(
            "POST", "/v1/projects/alpha/ops/task.create", token=token, body=body
        )
        assert status == 400 and reply["error"]["code"] == "VALIDATION_ERROR"
        assert not [x for x in server.log_lines if x["event"] == "op_crashed"]


def test_an_oversized_actor_name_is_refused_before_it_reaches_the_log(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        before = board_hash(root, "alpha")
        status, _, body = server.op(
            "alpha", "task.create", {"title": "x"}, token=token, actor_name="n" * 100_000
        )
        assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
        assert board_hash(root, "alpha") == before
        assert max(len(line) for line in server.log_stream.getvalue().splitlines()) < 2000


def test_a_body_budget_below_the_body_limit_is_refused() -> None:
    with pytest.raises(ServerConfigError):
        parse_config({"limits": {"token_body_bytes_per_minute": 10, "max_body_bytes": 100}})
    with pytest.raises(ServerConfigError):
        parse_config({"limits": {"token_body_bytes_per_minute": 0}})
