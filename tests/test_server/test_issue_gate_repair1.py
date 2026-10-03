"""A project whose only issue file is an empty-map ``ids.json`` still gates 0.2.1 clients."""

from __future__ import annotations

from pathlib import Path

from lattice.server.testing import make_root, open_stream, running_server
from tests.test_server.conftest import NO_AUDIT, mint

OLD = {"Lattice-Client-Version": "0.2.1"}


def test_empty_id_map_gates_old_clients_on_sync_and_stream(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}}, config=NO_AUDIT)
    issues = root / "projects" / "alpha" / ".lattice" / "issues"
    issues.mkdir(parents=True)
    (issues / "ids.json").write_text('{"schema_version":1,"next_seq":1,"map":{}}\n')
    token = mint(root)
    with running_server(root) as server:
        status, _, body = server.request(
            "GET", "/v1/projects/alpha/sync?since=0", token=token, headers=OLD
        )
        assert status == 400 and body["error"]["code"] == "CLIENT_TOO_OLD", body
        status, _, body = server.request("GET", "/v1/projects/alpha/sync?since=0", token=token)
        assert status == 200, body

        reader = open_stream(server.url, "alpha", token, headers=OLD)
        try:
            assert reader.status == 400
        finally:
            reader.close()
