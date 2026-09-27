"""The change stream carries Unicode and lone-surrogate event data end to end
(H-12 review round 2, item 3).

SSE frames escape non-ASCII (a lone surrogate has no UTF-8 encoding). A
subscriber decodes the event data exactly. Each entry's id carries its journal
line's hash, which must match the journal bytes on disk and the ``head_hash``
sync reports: the journal line bytes stay the one source of truth. A real
follower parses the entries and brings its cache to the server's head.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from lattice.remote.config import resolve_remote
from lattice.remote.follower import Follower, parse_stream_event
from lattice.remote.stream import open_stream
from lattice.server.journal import line_hash
from lattice.server.testing import wait_for
from tests.test_remote.hosted import HostedEnv, events_of, make_repo, run_cli

TEXTS = ("héllo ✓ 漢字", "\ud800 alone")


def test_unicode_and_lone_surrogate_events_round_trip_through_the_stream(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    writer = make_repo(tmp_path / "writer")
    follower_repo = make_repo(tmp_path / "follower")
    for repo in (writer, follower_repo):
        assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(writer, "create", "Streamed", "--actor", "agent:dev").exit_code == 0

    remote = resolve_remote("team")
    conn = open_stream(remote, "demo", last_event_id=None, timeout=10)
    received: list = []
    reader = threading.Thread(
        target=lambda: received.extend(e for e in _until_journal(conn.events(), 2)),
        daemon=True,
    )
    reader.start()
    follower = Follower(follower_repo, remote, "demo", heartbeat_seconds=0.5)
    following = threading.Thread(target=follower.run, daemon=True)
    following.start()
    try:
        assert wait_for(lambda: follower.stream_connects >= 1, 10)
        for text in TEXTS:
            data = json.dumps({"k": text})  # ASCII escapes: \\u00e9, \\ud800 ...
            result = run_cli(
                writer, "event", "DEM-1", "x_text", "--data", data, "--actor", "agent:dev"
            )
            assert result.exit_code == 0, result.output
        reader.join(timeout=15)
        assert not reader.is_alive(), "the stream delivered fewer than two entries"
        server_events = events_of(hosted_env, "DEM-1")
        head = server_events[-1]
        assert wait_for(
            lambda: (
                _cache_head(follower_repo) == hosted_env.handle.project("demo").journal.head_seq
            ),  # type: ignore[union-attr]
            10,
        )
    finally:
        conn.close()
        follower.stop()
        following.join(timeout=10)

    journal_lines = (hosted_env.board / "hosted" / "journal.jsonl").read_bytes().splitlines()
    status, _, sync = hosted_env.handle.request(  # type: ignore[union-attr]
        "GET", "/v1/projects/demo/sync?since=0", token=hosted_env.token
    )
    assert status == 200
    for event, text in zip(received, TEXTS, strict=True):
        body = json.loads(event.data)
        assert [e["data"] for e in body["events"]] == [{"k": text}]
        entry = parse_stream_event(event)  # the follower's own parser
        epoch, seq, digest = event.id.split(":")
        assert entry is not None and (entry.epoch, entry.seq) == (epoch, int(seq))
        assert int(seq) == body["seq"]
        assert digest == line_hash(journal_lines[int(seq) - 1])
    last_digest = received[-1].id.split(":")[2]
    assert sync["data"]["head_hash"] == last_digest == line_hash(journal_lines[-1])
    assert follower.ignored_entries == 0
    assert follower.deliveries["journal"] >= 2
    # The follower's cache holds the events exactly, the surrogate included.
    cached = [
        json.loads(line)
        for line in (follower_repo / ".lattice" / "events" / f"{head['task_id']}.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [e["data"] for e in cached if e["type"] == "x_text"] == [{"k": t} for t in TEXTS]
    shown = run_cli(follower_repo, "show", "DEM-1", "--json")
    assert shown.exit_code == 0, shown.output


def _cache_head(repo: Path) -> int | None:
    try:
        return json.loads((repo / ".lattice" / "cache" / "state.json").read_text())["head_seq"]
    except (OSError, ValueError, KeyError):
        return None


def _until_journal(events, count: int):  # noqa: ANN001, ANN202
    seen = 0
    for event in events:
        if event.event == "journal":
            yield event
            seen += 1
            if seen == count:
                return
