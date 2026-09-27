"""The checks behind two end-to-end cases H-12 proves over HTTP: the task.event data
cap (AC-15) and the server-chosen artifact payload name (G-1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.core.artifacts import payload_storage_name
from lattice.core.errors import OpError
from lattice.server.limits import check_event_data_cap
from tests.test_remote.hosted import hosted_env  # noqa: F401 - fixture


def test_event_data_cap() -> None:
    check_event_data_cap("task.event", {"data": {"k": "v" * 10}}, 64)
    with pytest.raises(OpError) as exc:
        check_event_data_cap("task.event", {"data": {"k": "v" * 100}}, 64)
    assert exc.value.code == "PAYLOAD_TOO_LARGE" and exc.value.http_status == 413
    # canonical JSON: key order and whitespace do not change the size
    data = {"b": 1, "a": [1, 2]}
    size = len('{"a":[1,2],"b":1}')
    check_event_data_cap("task.event", {"data": data}, size)
    with pytest.raises(OpError):
        check_event_data_cap("task.event", {"data": data}, size - 1)
    # multi-byte characters count as bytes
    with pytest.raises(OpError):
        check_event_data_cap("task.event", {"data": "é" * 40}, 64)
    # --data travels as JSON text: its data is measured, not the text as typed
    text = '{ "b" : 1,  "a" : [1, 2] }'
    check_event_data_cap("task.event", {"data": text}, size)
    with pytest.raises(OpError):
        check_event_data_cap("task.event", {"data": text}, size - 1)
    # other operations and events without data are not checked
    check_event_data_cap("task.comment", {"data": "x" * 1000}, 1)
    check_event_data_cap("task.event", {"type": "x_t"}, 1)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("../../x.md", "art_1.md"),
        ("/etc/passwd", "art_1"),
        ("notes.tar.gz", "art_1.gz"),
        ("..", "art_1"),
        ("a/b/../../c.txt", "art_1.txt"),
        ("x.m\\d", "art_1"),
        ("x.m\x00d", "art_1"),
        ("", "art_1"),
        ("report", "art_1"),
    ],
)
def test_payload_name_is_server_chosen(filename: str, expected: str) -> None:
    name = payload_storage_name("art_1", filename)
    assert name == expected
    assert "/" not in name and ".." not in name


# ---------------------------------------------------------------------------
# End to end (H-12): through a bound checkout
# ---------------------------------------------------------------------------


def _event_data(size: int) -> str:
    """``--data`` text (with the spaces a person types) whose canonical JSON is *size* bytes."""
    overhead = len('{"k":""}')
    return '{"k": "' + "v" * (size - overhead) + '"}'


def test_task_event_data_cap_through_a_bound_checkout(hosted_env, tmp_path: Path) -> None:  # noqa: F811
    """AC-15: ``lattice event`` with data over ``max_event_data_bytes`` fails with
    ``PAYLOAD_TOO_LARGE`` and writes nothing; data at the limit is accepted. The
    limit applies to the data's canonical JSON, not to how the text was typed."""
    from tests.test_remote.hosted import events_of, make_repo, run_cli

    limit = 64 * 1024
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Evented", "--actor", "agent:dev").exit_code == 0
    before = len(events_of(hosted_env, "DEM-1"))

    over = run_cli(
        repo, "event", "DEM-1", "x_big", "--data", _event_data(limit + 1), "--actor", "agent:dev"
    )
    assert over.exit_code == 1
    assert "PAYLOAD_TOO_LARGE" not in over.stdout  # plain output: the message only
    assert f"this server's limit is {limit} bytes" in over.stderr
    as_json = run_cli(
        repo,
        "event",
        "DEM-1",
        "x_big",
        "--data",
        _event_data(limit + 1),
        "--actor",
        "agent:dev",
        "--json",
    )
    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert len(events_of(hosted_env, "DEM-1")) == before

    at_limit = run_cli(
        repo, "event", "DEM-1", "x_big", "--data", _event_data(limit), "--actor", "agent:dev"
    )
    assert at_limit.exit_code == 0, at_limit.output
    assert events_of(hosted_env, "DEM-1")[-1]["type"] == "x_big"
