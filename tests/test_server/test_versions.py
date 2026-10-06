"""AC-48 (server side): protocol, unknown operations, unsupported params, old clients."""

from __future__ import annotations

from pathlib import Path
import tomllib

from lattice.server.protocol import MIN_CLIENT_VERSION, server_version
from lattice.server.testing import ServerHandle
from tests.test_server.conftest import board_hash, mint


def test_protocol_mismatch_is_refused_before_anything_runs(
    server: ServerHandle, root: Path
) -> None:
    token = mint(root)
    before = board_hash(root, "alpha")
    status, _, body = server.request(
        "POST",
        "/v1/projects/alpha/ops/task.create",
        token=token,
        body={"params": {"title": "x"}},
        headers={"Lattice-Protocol": "2"},
    )
    assert status == 400 and body["error"]["code"] == "PROTOCOL_MISMATCH"
    assert board_hash(root, "alpha") == before
    status, _, body = server.request(
        "GET", "/v1/info", token=token, headers={"Lattice-Protocol": "2"}
    )
    assert status == 400 and body["error"]["code"] == "PROTOCOL_MISMATCH"
    status, _, _ = server.request(
        "GET", "/v1/info", token=token, headers={"Lattice-Protocol": "1"}
    )
    assert status == 200


def test_unknown_op_names_both_versions(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    status, _, body = server.request(
        "POST",
        "/v1/projects/alpha/ops/task.teleport",
        token=token,
        body={"params": {}},
        headers={"Lattice-Client-Version": "9.9.9"},
    )
    assert status == 404 and body["error"]["code"] == "UNKNOWN_OP"
    message = body["error"]["message"]
    assert "task.teleport" in message and "9.9.9" in message and server_version() in message


def test_unsupported_param_names_the_param_and_both_versions(
    server: ServerHandle, root: Path
) -> None:
    token = mint(root)
    before = board_hash(root, "alpha")
    status, _, body = server.request(
        "POST",
        "/v1/projects/alpha/ops/task.create",
        token=token,
        body={"params": {"title": "x", "color": "red"}},
        headers={"Lattice-Client-Version": "9.9.9"},
    )
    assert status == 400 and body["error"]["code"] == "UNSUPPORTED_PARAM"
    message = body["error"]["message"]
    assert "'color'" in message and "9.9.9" in message and server_version() in message
    assert board_hash(root, "alpha") == before
    # the same newer client omitting its defaulted parameter is accepted
    status, _, _ = server.request(
        "POST",
        "/v1/projects/alpha/ops/task.create",
        token=token,
        body={"params": {"title": "x"}},
        headers={"Lattice-Client-Version": "9.9.9"},
    )
    assert status == 200


def test_defaulted_params_may_be_omitted(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    status, _, body = server.op("alpha", "xtest.defaulted", {"task": "ALP-1"}, token=token)
    assert status == 200 and body["data"]["result"]["value"] == {"color": "blue"}


def test_client_too_old(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    before = board_hash(root, "alpha")
    status, headers, body = server.request(
        "POST",
        "/v1/projects/alpha/ops/task.create",
        token=token,
        body={"params": {"title": "x"}},
        headers={"Lattice-Client-Version": "0.2.1"},
    )
    assert status == 400 and body["error"]["code"] == "CLIENT_TOO_OLD"
    assert "0.2.1" in body["error"]["message"] and MIN_CLIENT_VERSION in body["error"]["message"]
    assert headers["lattice-min-client-version"] == MIN_CLIENT_VERSION
    assert board_hash(root, "alpha") == before


def test_floor_stays_022_while_the_package_is_023(server: ServerHandle, root: Path) -> None:
    """The minimum client marks the issue-path capability floor, not the release."""
    project_root = Path(__file__).resolve().parents[2]
    with (project_root / "pyproject.toml").open("rb") as stream:
        package = tomllib.load(stream)
    assert MIN_CLIENT_VERSION == "0.2.2"
    assert package["project"]["version"] == "0.2.3"
    status, headers, body = server.request("GET", "/v1/info", token=mint(root))
    assert status == 200
    assert body["data"]["min_client_version"] == "0.2.2"
    assert headers["lattice-min-client-version"] == "0.2.2"


def test_malformed_requests(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    cases = [
        ({"params": {"title": 5}}, "VALIDATION_ERROR"),
        ({"params": {}}, "VALIDATION_ERROR"),
        ({"params": "x"}, "VALIDATION_ERROR"),
        ({"params": {"title": "x"}, "surprise": 1}, "VALIDATION_ERROR"),
        ({"params": {"title": "x"}, "expect": {"other": 1}}, "VALIDATION_ERROR"),
        ({"params": {"title": "x"}, "attestations": []}, "VALIDATION_ERROR"),
        ({"params": {"title": "x"}, "actor": 5}, "VALIDATION_ERROR"),
    ]
    for body_in, code in cases:
        status, _, body = server.request(
            "POST", "/v1/projects/alpha/ops/task.create", token=token, body=body_in
        )
        assert status == 400 and body["error"]["code"] == code, (body_in, body)
    status, _, body = server.request(
        "POST", "/v1/projects/alpha/ops/task.create", token=token, body=b"{not json"
    )
    assert status == 400
    status, _, body = server.request(
        "POST",
        "/v1/projects/alpha/ops/task.create",
        token=token,
        body=b'{"params": {"title": "x"}}',
        headers={"Content-Type": "text/plain"},
    )
    assert status == 400 and "Content-Type" in body["error"]["message"]
