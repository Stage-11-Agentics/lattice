"""The checks behind two end-to-end cases H-12 proves over HTTP: the task.event data
cap (AC-15) and the server-chosen artifact payload name (G-1)."""

from __future__ import annotations

import pytest

from lattice.core.artifacts import payload_storage_name
from lattice.core.errors import OpError
from lattice.server.limits import check_event_data_cap


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
