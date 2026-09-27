"""TEST-ONLY sync shim: SPEC §8.8 ``sync`` on the real in-process server.

H-11 was built before H-10a (server sync and stream) landed. Its end-to-end
tests need one real server that takes operations *and* answers ``sync``, so
this shim mounts a ``GET /v1/projects/{slug}/sync`` route on
``lattice.server.testing.running_server``'s app. It reads the project's real
journal under the project's work lock and answers in the wire format of §8.8:

- ``since == head`` with a matching epoch and hash: an empty delta;
- another epoch, a ``since`` past the head, or a hash mismatch: a reset holding
  every synced file inline;
- otherwise the files each journal entry after ``since`` lists, whole and
  inline (no append deltas), and the listed paths that no longer exist as
  ``removed``;
- ``manifest=1``: every synced file's hash and size (doctor on a cache).

It is replaced by H-10a's real routes when H-11 rebases onto them.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from starlette.routing import Route

from lattice.server import app as server_app
from tests.test_remote.stub_sync_server import durable_files


def _file(data: bytes, *, inline: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    if inline:
        entry["content_b64"] = base64.b64encode(data).decode("ascii")
    return entry


def assemble(project: Any, query: dict[str, str]) -> dict[str, Any]:
    journal = project.journal
    board = project.board
    head = journal.head_seq
    body: dict[str, Any] = {"epoch": journal.epoch, "head_seq": head}
    if journal.head_hash:
        body["head_hash"] = journal.head_hash
    if query.get("manifest") == "1":
        files = {
            rel: _file(p.read_bytes(), inline=False) for rel, p in durable_files(board).items()
        }
        body.update(reset=True, files=files, removed=[])
        return body
    try:
        since = int(query.get("since") or 0)
    except ValueError:
        since = -1
    reset = (
        since < 0
        or query.get("epoch") != journal.epoch
        or since > head
        or (since > 0 and query.get("hash") != journal.line_hashes[since - 1])
    )
    files: dict[str, dict] = {}
    removed: list[str] = []
    if reset:
        files = {rel: _file(p.read_bytes()) for rel, p in sorted(durable_files(board).items())}
    else:
        synced = durable_files(board)
        touched: set[str] = set()
        for entry in journal.read_entries(since):
            touched.update(entry.get("paths") or [])
        for rel in sorted(touched):
            path = board / rel
            if path.is_dir():
                continue
            if rel in synced:
                files[rel] = _file(synced[rel].read_bytes())
            elif not path.exists():
                removed.append(rel)
    body.update(reset=reset, files=files, removed=removed)
    return body


async def sync_route(request: Any, state: Any) -> Any:
    slug = request.path_params["slug"]

    async def run(token: Any) -> Any:
        project = server_app.resolve_project(state, token, slug)
        query = dict(request.query_params)
        body = await state.registry.run_locked(
            project, lambda: assemble(project, query), admit=False
        )
        return server_app.envelope_ok(body)

    return await server_app._with_token(request, state, run)


def install(app: Any) -> None:
    """Mount the shim's ``sync`` route on a running server's *app*."""
    inner = app.app if hasattr(app, "app") else app  # under HeadersMiddleware
    inner.router.routes.insert(
        0,
        Route(
            "/v1/projects/{slug}/sync",
            server_app.endpoint(sync_route),
            methods=["GET"],
        ),
    )
