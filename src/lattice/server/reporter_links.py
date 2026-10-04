"""Public, link-scoped issue filing for hosted projects (LAT-390).

Everything below ``/r/`` is an explicit allowlist. The route resolves one
server-root link secret to its bound filing token before it looks up a project;
the only board write is that token's ``issue.file`` operation.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit, urlunsplit

from filelock import FileLock, Timeout as FileLockTimeout
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from lattice.core.errors import OpError
from lattice.core.events import utc_now
from lattice.core.ids import generate_instance_id
from lattice.server import admin, control
from lattice.storage.fs import atomic_write, ensure_dir

REGISTRY_NAME = "reporter_links.json"
STAGE_MAP_NAME = "reporter_link_stages.json"
LOCKS_DIR = ".reporter-link-locks"
LINK_ID_RE = re.compile(r"^link_[0-9A-HJKMNP-TV-Z]{26}$")
LINK_SECRET_RE = re.compile(r"^rpt_[A-Za-z0-9_-]{43}$")
SOURCE_REF_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_ID_RE = re.compile(r"^tok_[0-9A-HJKMNP-TV-Z]{26}$")
SERVICE_ACTOR = "agent:reporter-link"
OPS_PER_MINUTE = 30
BYTES_PER_MINUTE = 268435456
MAX_STAGED_BYTES = 536870912
RECEIPT_FIELDS = (
    "id",
    "short_id",
    "filed_at",
    "source",
    "source_ref",
    "external",
    "deduplicated",
)
NOT_FOUND_BODY = b"Not found.\n"
REPORTER_COPY = {
    "PHOTO_METADATA_UNSTRIPPED": "This photo could not be made private. Try another photo.",
    "MEDIA_STAGE_UNAVAILABLE": "This server could not safely prepare that video. Try a different video or send the team a note.",
    "CONFLICT": "That file conflicts with an earlier upload. Choose it again and retry.",
    "BOARD_BUSY": "The team’s issue board is busy. Please wait a moment and retry.",
    "ISSUES_DISABLED": "This team is not accepting issue reports right now.",
    "RATE_LIMITED": "Too many uploads or reports right now. Please wait a moment and retry.",
    "PAYLOAD_TOO_LARGE": "That file or report is too large. Remove a file or choose a smaller one.",
    "MEDIA_QUOTA_EXCEEDED": "This report has reached its media limit. Remove a file or try again later.",
    "FORBIDDEN": "This report link cannot be used from this page.",
}
GENERIC_COPY = "We couldn’t send this report. Check the form and try again."


class _LinkUnavailable(Exception):
    """A link was revoked between request receipt and its protected commit."""


def _registry_path(root: Path) -> Path:
    return Path(root) / REGISTRY_NAME


def _valid_record(record: object) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("id"), str)
        and LINK_ID_RE.fullmatch(record["id"]) is not None
        and isinstance(record.get("project"), str)
        and admin.SLUG_RE.fullmatch(record["project"]) is not None
        and isinstance(record.get("label"), str)
        and 1 <= len(record["label"]) <= 256
        and record["label"].isprintable()
        and isinstance(record.get("secret_sha256"), str)
        and SHA256_RE.fullmatch(record["secret_sha256"]) is not None
        and isinstance(record.get("token_id"), str)
        and TOKEN_ID_RE.fullmatch(record["token_id"]) is not None
        and isinstance(record.get("public_base_url"), str)
        and isinstance(record.get("public_origin"), str)
        and isinstance(record.get("created_at"), str)
        and (record.get("revoked_at") is None or isinstance(record.get("revoked_at"), str))
    )


def _read_registry(root: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(_registry_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise OpError("INTEGRITY_ERROR", "reporter link registry is unreadable.") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise OpError("INTEGRITY_ERROR", "reporter link registry has an invalid format.")
    links = raw.get("links")
    if not isinstance(links, list) or not all(_valid_record(item) for item in links):
        raise OpError("INTEGRITY_ERROR", "reporter link registry has an invalid entry.")
    return links


def _write_registry(root: Path, links: list[dict[str, Any]]) -> None:
    path = _registry_path(root)
    atomic_write(
        path,
        json.dumps({"schema_version": 1, "links": links}, sort_keys=True, indent=2) + "\n",
    )
    os.chmod(path, 0o600)


def _stage_map_key(link_id: str, source_ref: str) -> str:
    return f"{link_id}:{source_ref}"


def _read_stage_map(root: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads((Path(root) / STAGE_MAP_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise OpError("INTEGRITY_ERROR", "reporter stage references are unreadable.") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise OpError("INTEGRITY_ERROR", "reporter stage references have an invalid format.")
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        raise OpError("INTEGRITY_ERROR", "reporter stage references have an invalid format.")
    for key, entry in entries.items():
        if (
            not isinstance(key, str)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("link_id"), str)
            or LINK_ID_RE.fullmatch(entry["link_id"]) is None
            or not isinstance(entry.get("source_ref"), str)
            or SOURCE_REF_RE.fullmatch(entry["source_ref"]) is None
            or key != _stage_map_key(entry["link_id"], entry["source_ref"])
            or not isinstance(entry.get("hashes"), list)
            or not all(
                isinstance(digest, str) and SHA256_RE.fullmatch(digest)
                for digest in entry["hashes"]
            )
            or not isinstance(entry.get("updated_at"), (int, float))
            or isinstance(entry.get("updated_at"), bool)
        ):
            raise OpError("INTEGRITY_ERROR", "reporter stage references have an invalid entry.")
    return entries


def _write_stage_map(root: Path, entries: dict[str, dict[str, Any]]) -> None:
    path = Path(root) / STAGE_MAP_NAME
    atomic_write(
        path,
        json.dumps({"schema_version": 1, "entries": entries}, sort_keys=True, indent=2) + "\n",
    )
    os.chmod(path, 0o600)


def _prune_stage_map(entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from lattice.server.issue_media import STAGE_TTL_SECONDS

    now = time.time()
    return {
        key: entry
        for key, entry in entries.items()
        if now - float(entry["updated_at"]) < STAGE_TTL_SECONDS
    }


def _record_source_stages(root: Path, link_id: str, source_ref: str, hashes: set[str]) -> None:
    hashes = {digest for digest in hashes if SHA256_RE.fullmatch(digest)}
    if not hashes:
        return
    with admin.admin_lock(root):
        entries = _prune_stage_map(_read_stage_map(root))
        key = _stage_map_key(link_id, source_ref)
        current = entries.get(
            key,
            {"link_id": link_id, "source_ref": source_ref, "hashes": [], "updated_at": 0},
        )
        entries[key] = {
            **current,
            "hashes": sorted(set(current["hashes"]) | hashes),
            "updated_at": time.time(),
        }
        _write_stage_map(root, entries)


def _forget_source_stages_under_admin_lock(root: Path, link_id: str, source_ref: str) -> set[str]:
    original = _read_stage_map(root)
    entries = _prune_stage_map(original)
    key = _stage_map_key(link_id, source_ref)
    removed = entries.pop(key, None)
    if removed is None:
        if len(entries) != len(original):
            _write_stage_map(root, entries)
        return set()
    referenced_elsewhere = {
        digest
        for entry in entries.values()
        if entry["link_id"] == link_id
        for digest in entry["hashes"]
    }
    _write_stage_map(root, entries)
    return set(removed["hashes"]) - referenced_elsewhere


def _other_source_stage_hashes_under_admin_lock(
    root: Path, link_id: str, source_ref: str, submitted_hashes: set[str]
) -> frozenset[str]:
    """Find submitted hashes another live report for this link still needs."""
    entries = _prune_stage_map(_read_stage_map(root))
    return frozenset(
        digest
        for entry in entries.values()
        if entry["link_id"] == link_id and entry["source_ref"] != source_ref
        for digest in entry["hashes"]
        if digest in submitted_hashes
    )


def _forget_link_stages_under_admin_lock(root: Path, link_id: str) -> None:
    original = _read_stage_map(root)
    entries = _prune_stage_map(original)
    remaining = {key: entry for key, entry in entries.items() if entry["link_id"] != link_id}
    if len(remaining) != len(original):
        _write_stage_map(root, remaining)


def _origin(parsed) -> str:
    host = (parsed.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is not None and not (
        (parsed.scheme.lower() == "https" and port == 443)
        or (parsed.scheme.lower() == "http" and port == 80)
    ):
        host = f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{host}"


def validate_public_base_url(value: str) -> tuple[str, str]:
    """Return a validated slash-terminated base URL and its separately stored origin."""
    if not isinstance(value, str) or value != value.strip() or not value:
        raise OpError("VALIDATION_ERROR", "--public-base-url must be an absolute HTTPS URL.")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise OpError("VALIDATION_ERROR", "--public-base-url is not a valid URL.") from None
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"https", "http"}
        or not parsed.netloc
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not host.isascii()
        or any(char.isspace() for char in parsed.netloc)
    ):
        raise OpError(
            "VALIDATION_ERROR",
            "--public-base-url must be HTTPS with no credentials, query, or fragment.",
        )
    if scheme == "http":
        loopback = host.lower() == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if not loopback:
            raise OpError(
                "VALIDATION_ERROR", "HTTP public base URLs are allowed only on loopback."
            )
    if port is not None and not (1 <= port <= 65535):
        raise OpError("VALIDATION_ERROR", "--public-base-url has an invalid port.")
    path = unquote(parsed.path or "/")
    if (
        "\\" in path
        or any(part in {".", ".."} for part in path.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or re.search(r"%(?:2f|5c)", parsed.path, flags=re.I)
        or not (path == "/" or re.fullmatch(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*/?", path))
    ):
        raise OpError("VALIDATION_ERROR", "--public-base-url path prefix is unsafe.")
    if not path.endswith("/"):
        path += "/"
    base = urlunsplit((scheme, parsed.netloc, path, "", ""))
    return base, _origin(parsed)


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record[key]
        for key in (
            "id",
            "project",
            "label",
            "token_id",
            "public_base_url",
            "public_origin",
            "created_at",
            "revoked_at",
        )
    }


def create_link(root: Path, project: str, label: str, public_base_url: str) -> dict[str, Any]:
    """Create a link and its exactly scoped filing-only token."""
    from lattice.server import tokens

    root = Path(root)
    admin.require_root(root)
    admin.existing_project(root, project)
    normalized_label = label.strip() if isinstance(label, str) else ""
    if not normalized_label or len(normalized_label) > 256 or not normalized_label.isprintable():
        raise OpError("VALIDATION_ERROR", "--label must be 1-256 printable characters.")
    base_url, public_origin = validate_public_base_url(public_base_url)
    link_id = "link_" + generate_instance_id().removeprefix("inst_")
    secret = "rpt_" + secrets.token_urlsafe(32)
    source = f"reporter-link:{link_id}"
    minted = tokens.create_token(
        root,
        user="human:reporter-link-admin",
        machine="reporter-link",
        actors=(SERVICE_ACTOR,),
        projects=(project,),
        only=("issue.file",),
        source=source,
        ops_per_minute=OPS_PER_MINUTE,
        bytes_per_minute=BYTES_PER_MINUTE,
        max_staged_bytes=MAX_STAGED_BYTES,
    )
    record = {
        "id": link_id,
        "project": project,
        "label": normalized_label,
        "secret_sha256": hashlib.sha256(secret.encode("ascii")).hexdigest(),
        "token_id": minted["record"]["id"],
        "public_base_url": base_url,
        "public_origin": public_origin,
        "created_at": utc_now(),
        "revoked_at": None,
    }
    try:
        with admin.admin_lock(root):
            links = _read_registry(root)
            links.append(record)
            _write_registry(root, links)
    except Exception as exc:
        try:
            tokens.revoke_token(root, record["token_id"])
        except Exception:
            pass
        raise OpError(
            "WRITE_ERROR", "Could not save reporter link; its filing token was revoked."
        ) from exc
    return {"link": _public_record(record), "url": f"{base_url}r/{secret}/"}


def list_links(root: Path) -> list[dict[str, Any]]:
    root = Path(root)
    admin.require_root(root)
    with admin.admin_lock(root):
        return [_public_record(record) for record in _read_registry(root)]


def _link_lock_path(root: Path, link_id: str) -> Path:
    if not LINK_ID_RE.fullmatch(link_id):
        raise OpError("NOT_FOUND", "reporter link not found.")
    folder = Path(root) / LOCKS_DIR
    ensure_dir(folder)
    os.chmod(folder, 0o700)
    return folder / f"{link_id}.lock"


@contextmanager
def _link_lock(root: Path, link_id: str, *, timeout: float = 30.0) -> Iterator[None]:
    lock = FileLock(str(_link_lock_path(root, link_id)), timeout=timeout, mode=0o600)
    try:
        with lock:
            yield
    except FileLockTimeout:
        raise OpError(
            "BOARD_BUSY", "this reporter link is busy; retry shortly.", {"retry_after": 2}
        ) from None


def _revoke_token_while_admin_locked(root: Path, token_id: str) -> None:
    """Update tokens.json without nesting a second admin-lock acquisition."""
    from lattice.server import tokens

    records = tokens._read(root)
    for index, record in enumerate(records):
        if record.id == token_id:
            if record.revoked_at is None:
                records[index] = replace(record, revoked_at=utc_now())
                tokens._write(root, records)
            return


def _offline_cleanup(root: Path, record: dict[str, Any]) -> None:
    from lattice.server.config import load_config
    from lattice.server.issue_media import HostedIssueMedia

    project_dir = root / "projects" / record["project"]
    board = project_dir / ".lattice"
    limits = load_config(root).limits
    media = HostedIssueMedia(
        project_dir,
        board,
        max_file_bytes=limits.max_issue_media_file_bytes,
        max_issue_bytes=limits.max_issue_media_issue_bytes,
        max_project_bytes=limits.max_issue_media_project_bytes,
    )
    media.cleanup_token_stages(record["token_id"])


def revoke_link(root: Path, link_id: str) -> dict[str, Any]:
    """Revoke under link/admin locks, then clean token-owned stages safely."""
    root = Path(root)
    admin.require_root(root)
    with _link_lock(root, link_id, timeout=120.0):
        with admin.admin_lock(root):
            links = _read_registry(root)
            index = next((i for i, row in enumerate(links) if row["id"] == link_id), None)
            if index is None:
                raise OpError("NOT_FOUND", "reporter link not found.")
            record = links[index]
            _revoke_token_while_admin_locked(root, record["token_id"])
            if record["revoked_at"] is None:
                record = {**record, "revoked_at": utc_now()}
                links[index] = record
                _write_registry(root, links)
            _forget_link_stages_under_admin_lock(root, link_id)

    # The link lock is gone before either cleanup path. Holding server.lock
    # excludes a server start racing with direct filesystem cleanup.
    lease_fd = control.try_server_lock(root)
    if lease_fd is None:
        board = root / "projects" / record["project"] / ".lattice"
        answer = control.send_request(
            board,
            "cleanup-reporter-link-stages",
            {"token_id": record["token_id"]},
        )
        if not answer.get("ok"):
            error = answer.get("error") or {}
            raise OpError(
                error.get("code", "BOARD_BUSY"), "Link revoked; media cleanup is pending."
            )
    else:
        try:
            _offline_cleanup(root, record)
        finally:
            os.close(lease_fd)
    return _public_record(record)


def _record_for_secret(root: Path, secret: str) -> dict[str, Any] | None:
    if not LINK_SECRET_RE.fullmatch(secret):
        return None
    digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
    for record in _read_registry(root):
        if secrets.compare_digest(record["secret_sha256"], digest):
            return record
    return None


def _live_token(token_store: Any, record: dict[str, Any]):
    token = token_store.get(record["token_id"])
    if (
        token is None
        or token.revoked_at is not None
        or not token.filing_only
        or token.only != ("issue.file",)
        or token.projects != (record["project"],)
        or token.source != f"reporter-link:{record['id']}"
        or token.actors != (SERVICE_ACTOR,)
        or token.effective_ops_per_minute(OPS_PER_MINUTE) != OPS_PER_MINUTE
        or token.effective_bytes_per_minute(BYTES_PER_MINUTE) != BYTES_PER_MINUTE
        or token.effective_max_staged_bytes() != MAX_STAGED_BYTES
    ):
        return None
    return token


def _live_record_and_token(root: Path, secret: str, token_store: Any):
    record = _record_for_secret(root, secret)
    if record is None or record["revoked_at"] is not None:
        return None, None
    token = _live_token(token_store, record)
    return (record, token) if token is not None else (None, None)


def _not_found(request: Request) -> Response:
    from lattice.server.app import _close_after_answer

    _close_after_answer(request, None)
    return Response(NOT_FOUND_BODY, status_code=404, media_type="text/plain; charset=utf-8")


def _canonical(request: Request, expected: str, *, allow_query: bool = False) -> bool:
    raw_path = request.scope.get("raw_path")
    if not isinstance(raw_path, bytes) or not expected.isascii():
        return False
    raw_only = raw_path.split(b"?", 1)[0]
    return (
        raw_only == expected.encode("ascii")
        and request.scope.get("path") == expected
        and (allow_query or request.scope.get("query_string", b"") == b"")
    )


def _request_source_ref(value: object) -> bool:
    return isinstance(value, str) and SOURCE_REF_RE.fullmatch(value) is not None


def _plain_error(exc: OpError) -> OpError:
    code = exc.code
    if code == "VALIDATION_ERROR" and exc.details.get("reason") == "PHOTO_METADATA_UNSTRIPPED":
        code = "PHOTO_METADATA_UNSTRIPPED"
    message = REPORTER_COPY.get(code, GENERIC_COPY)
    details = {}
    retry_after = exc.details.get("retry_after")
    if retry_after is not None:
        details["retry_after"] = retry_after
    return OpError(code, message, details)


def _ensure_no_authorization(request: Request) -> bool:
    return request.headers.get("authorization") is None


async def _resolve_request_link(request: Request, state: Any):
    from lattice.server.registry import in_worker

    secret = request.path_params.get("secret", "")
    try:
        record, token = await in_worker(
            lambda: _live_record_and_token(state.root, secret, state.tokens)
        )
    except OpError:
        return None, None
    if record is None or token is None:
        return None, None
    return record, token


async def page(request: Request, state: Any) -> Response:
    from lattice.server.registry import in_worker

    secret = request.path_params["secret"]
    if not _canonical(request, f"/r/{secret}/") or not _ensure_no_authorization(request):
        return _not_found(request)
    record, token = await _resolve_request_link(request, state)
    if record is None or token is None:
        return _not_found(request)
    from lattice.dashboard.server import STATIC_DIR

    content = await in_worker(lambda: (STATIC_DIR / "reporter" / "index.html").read_bytes())
    return Response(content, media_type="text/html; charset=utf-8")


async def redirect(request: Request, state: Any) -> Response:
    secret = request.path_params["secret"]
    if not _canonical(request, f"/r/{secret}") or not _ensure_no_authorization(request):
        return _not_found(request)
    record, token = await _resolve_request_link(request, state)
    if record is None or token is None:
        return _not_found(request)
    return RedirectResponse(f"{secret}/", status_code=308)


async def asset(request: Request, state: Any) -> Response:
    from lattice.server.registry import in_worker

    secret = request.path_params["secret"]
    name = request.path_params["name"]
    if (
        name not in {"reporter.css", "reporter.js"}
        or not _canonical(request, f"/r/{secret}/{name}")
        or not _ensure_no_authorization(request)
    ):
        return _not_found(request)
    record, token = await _resolve_request_link(request, state)
    if record is None or token is None:
        return _not_found(request)
    from lattice.dashboard.server import STATIC_DIR

    content = await in_worker(lambda: (STATIC_DIR / "reporter" / name).read_bytes())
    media_type = (
        "text/css; charset=utf-8" if name.endswith(".css") else "text/javascript; charset=utf-8"
    )
    return Response(content, media_type=media_type)


async def missing(request: Request, _state: Any) -> Response:
    """The all-method `/r/` catchall: no board lookup and one generic answer."""
    return _not_found(request)


def _check_origin(request: Request, state: Any, record: dict[str, Any]) -> None:
    from lattice.server.web import origin_allowed

    origin = request.headers.get("origin")
    if not origin or origin == "null":
        raise OpError("FORBIDDEN", "reporter Origin refused.")
    if origin == record["public_origin"] or origin_allowed(request, state):
        return
    raise OpError("FORBIDDEN", "reporter Origin refused.")


def _effective_body_limit(state: Any, token: Any) -> int:
    return token.effective_bytes_per_minute(
        state.config.limits.token_body_bytes_per_minute,
        max_issue_media_file_bytes=state.config.limits.max_issue_media_file_bytes,
    )


def _take_link_operation(state: Any, token: Any) -> None:
    state.limits.take_op(
        token.id,
        token.effective_ops_per_minute(state.config.limits.token_ops_per_minute),
    )


def _project_for_link(state: Any, record: dict[str, Any]):
    # Called only after the secret and its exact token binding were checked.
    return state.registry.get(record["project"])


async def _cleanup_source_stages(
    state: Any,
    secret: str,
    record: dict[str, Any],
    token: Any,
    project: Any,
    source_ref: str,
) -> None:
    """Forget one logical report and release only hashes no sibling ref uses."""
    from lattice.server.registry import in_worker

    def cleanup() -> None:
        with _link_lock(
            state.root, record["id"], timeout=state.config.limits.lock_timeout_seconds
        ):
            current, live = _live_record_and_token(state.root, secret, state.tokens)
            if current is None or live is None or live.id != token.id:
                return  # revoke owns all stage cleanup after it marks the token dead
            with admin.admin_lock(state.root):
                hashes = _forget_source_stages_under_admin_lock(
                    state.root, record["id"], source_ref
                )
            if hashes:
                project.issue_media.cleanup_token_stages(token.id, hashes)

    await in_worker(cleanup)


def _retryable_link_error(code: str) -> bool:
    return code in {"RATE_LIMITED", "BOARD_BUSY", "STORAGE_LOW", "UPLOAD_TIMEOUT"}


def _safe_filename(value: str) -> str:
    from lattice.core.issue_media import clean_original_name

    name = clean_original_name(value) or "attachment"
    if len(name) > 256:
        raise OpError("VALIDATION_ERROR", "The file name is too long.")
    return name


async def media_stage(request: Request, state: Any) -> Response:
    from lattice.server import app as app_module
    from lattice.server.media_staging import prepare_media_file, stage_prepared_item

    secret = request.path_params["secret"]
    source_ref = request.path_params["source_ref"]
    digest = request.path_params["sha256"]
    expected = f"/r/{secret}/media/{source_ref}/{digest}"
    query = request.query_params
    if (
        not _canonical(request, expected, allow_query=True)
        or not _ensure_no_authorization(request)
        or not _request_source_ref(source_ref)
        or not SHA256_RE.fullmatch(digest)
        or any(key != "filename" for key in query.keys())
        or len(query.getlist("filename")) > 1
    ):
        return _not_found(request)
    record, token = await _resolve_request_link(request, state)
    if record is None or token is None:
        return _not_found(request)
    project = None
    origin_valid = False

    async def refuse(exc: OpError, body=None) -> None:  # noqa: ANN001
        app_module._close_after_answer(request, body)
        if origin_valid and not _retryable_link_error(exc.code):
            cleanup_project = project or _project_for_link(state, record)
            if cleanup_project is not None:
                try:
                    await _cleanup_source_stages(
                        state, secret, record, token, cleanup_project, source_ref
                    )
                except OpError as cleanup_error:
                    raise _plain_error(cleanup_error) from None
        raise _plain_error(exc) from None

    try:
        _check_origin(request, state, record)
        origin_valid = True
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/octet-stream"
        ):
            raise OpError("VALIDATION_ERROR", "Choose a photo or video file and try again.")
        declared = request.headers.get("content-length")
        if (
            declared is None
            or len(declared) > 20
            or not declared.isascii()
            or not declared.isdigit()
        ):
            raise OpError("VALIDATION_ERROR", "The upload needs a valid file size.")
        size = int(declared)
        file_limit = state.config.limits.max_issue_media_file_bytes
        body_limit = _effective_body_limit(state, token)
        if size > file_limit or size > body_limit:
            raise OpError("PAYLOAD_TOO_LARGE", "That file is over this link’s upload limit.")
    except OpError as exc:
        await refuse(exc)

    project = _project_for_link(state, record)
    if project is None:
        return _not_found(request)
    request.scope["state"]["log"].update(
        project=record["project"], token_id=token.id, op="reporter_link.media_stage"
    )
    body = app_module._UploadBody(request, state.log, record["project"], token.id)
    entered = False
    try:
        try:
            state.limits.enter(token.id)
        except OpError as exc:
            await refuse(exc, body)
        entered = True
        try:
            _take_link_operation(state, token)
            state.limits.take_bytes(token.id, size, body_limit)
            state.disk.check()
        except OpError as exc:
            await refuse(exc, body)

        received = bytearray()
        try:
            while (chunk := await body.next()) is not None:
                if body.received > size:
                    app_module._close_after_answer(request, body)
                    raise OpError("VALIDATION_ERROR", "The upload exceeded its declared size.")
                received.extend(chunk)
        except OpError as exc:
            await refuse(exc, body)
        if len(received) != size or hashlib.sha256(received).hexdigest() != digest:
            await refuse(
                OpError(
                    "VALIDATION_ERROR", "The upload did not match its declared size and hash."
                ),
                body,
            )

        try:
            filename = _safe_filename(request.query_params.get("filename", "attachment"))
            prepared = await app_module.in_worker(
                lambda: prepare_media_file(
                    filename,
                    bytes(received),
                    refuse_video_without_ffmpeg=True,
                    refuse_unstrippable_photos=True,
                )
            )

            def commit_stage() -> dict:
                with _link_lock(
                    state.root, record["id"], timeout=state.config.limits.lock_timeout_seconds
                ):
                    current, live = _live_record_and_token(state.root, secret, state.tokens)
                    if (
                        current is None
                        or live is None
                        or current["id"] != record["id"]
                        or live.id != token.id
                    ):
                        raise _LinkUnavailable
                    staged = stage_prepared_item(
                        project,
                        prepared,
                        token_id=live.id,
                        max_staged_bytes=live.effective_max_staged_bytes(),
                        dashboard=False,
                    )
                    _record_source_stages(
                        state.root,
                        record["id"],
                        source_ref,
                        _submitted_hashes([staged]),
                    )
                    return staged

            staged = await app_module.in_worker(commit_stage)
            return app_module.AsciiJSONResponse({"ok": True, "data": staged}, status_code=201)
        except _LinkUnavailable:
            return _not_found(request)
        except OpError as exc:
            await refuse(exc, body)
    finally:
        if entered:
            state.limits.leave(token.id)


def _reporter_description(body: dict[str, Any]) -> str | None:
    details = body.get("description")
    if details is not None and not isinstance(details, str):
        raise OpError("VALIDATION_ERROR", "What happened must be text.")
    if isinstance(details, str) and len(details) > 60000:
        raise OpError("VALIDATION_ERROR", "What happened is too long.")
    sections = [details.strip()] if isinstance(details, str) and details.strip() else []
    reporter_fields: list[str] = []
    for key, title in (("reporter_name", "Name"), ("reporter_email", "Email")):
        value = body.get(key, "")
        if not isinstance(value, str) or len(value) > 256 or (value and not value.isprintable()):
            raise OpError("VALIDATION_ERROR", f"Reporter {title.lower()} is not valid.")
        if value.strip():
            reporter_fields.append(f"{title}: {value.strip()}")
    if reporter_fields:
        sections.append("Reporter details:\n" + "\n".join(reporter_fields))
    return "\n\n".join(sections) or None


def _submitted_hashes(media: object) -> set[str]:
    hashes: set[str] = set()
    if not isinstance(media, list):
        return hashes
    for item in media:
        if not isinstance(item, dict):
            continue
        payloads = [item.get("payload")]
        frames = item.get("frames", [])
        if isinstance(frames, list):
            payloads.extend(frame.get("payload") for frame in frames if isinstance(frame, dict))
        for payload in payloads:
            if isinstance(payload, dict):
                digest = payload.get("sha256")
                if isinstance(digest, str) and SHA256_RE.fullmatch(digest):
                    hashes.add(digest)
    return hashes


async def submit(request: Request, state: Any) -> Response:
    from lattice.server import app as app_module

    secret = request.path_params["secret"]
    if not _canonical(request, f"/r/{secret}/submit") or not _ensure_no_authorization(request):
        return _not_found(request)
    record, token = await _resolve_request_link(request, state)
    if record is None or token is None:
        return _not_found(request)
    try:
        _check_origin(request, state, record)
    except OpError as exc:
        return (
            _not_found(request)
            if exc.code == "FORBIDDEN"
            else app_module.envelope_error(_plain_error(exc))
        )
    project = _project_for_link(state, record)
    if project is None:
        return _not_found(request)
    request.scope["state"]["log"].update(
        project=record["project"], token_id=token.id, op="issue.file"
    )
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        app_module._close_after_answer(request, None)
        return app_module.envelope_error(
            _plain_error(OpError("VALIDATION_ERROR", "form data required"))
        )

    body_limit = min(state.config.limits.max_body_bytes, _effective_body_limit(state, token))
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isascii() or not declared.isdigit() or len(declared) > 20:
            app_module._close_after_answer(request, None)
            return app_module.envelope_error(
                _plain_error(OpError("VALIDATION_ERROR", "invalid body size"))
            )
        if int(declared) > body_limit:
            app_module._close_after_answer(request, None)
            return app_module.envelope_error(
                _plain_error(OpError("PAYLOAD_TOO_LARGE", "body too large"))
            )

    entered = False
    source_ref = None
    media: list[dict] = []
    try:
        state.limits.enter(token.id)
        entered = True
        try:
            _take_link_operation(state, token)
            raw = await app_module.read_body(request, state, token)
        except OpError as exc:
            app_module._close_after_answer(request, None)
            raise exc
        try:
            form = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OpError("VALIDATION_ERROR", "invalid form data") from None
        allowed = {
            "op_id",
            "source_ref",
            "title",
            "description",
            "reporter_name",
            "reporter_email",
            "media",
        }
        if not isinstance(form, dict):
            raise OpError("VALIDATION_ERROR", "unsupported form fields")
        if not _request_source_ref(form.get("source_ref")):
            raise OpError("VALIDATION_ERROR", "invalid report reference")
        source_ref = form["source_ref"]
        if set(form) - allowed:
            raise OpError("VALIDATION_ERROR", "unsupported form fields")
        op_id = form.get("op_id")
        title = form.get("title")
        if (
            not isinstance(op_id, str)
            or not isinstance(title, str)
            or not title.strip()
            or len(title) > 200
        ):
            raise OpError("VALIDATION_ERROR", "add a report title")
        media_value = form.get("media", [])
        if not isinstance(media_value, list) or not all(
            isinstance(item, dict) for item in media_value
        ):
            raise OpError("VALIDATION_ERROR", "invalid report attachments")
        media = media_value
        description = _reporter_description(form)
        envelope = {
            "op_id": op_id,
            "origin": {"reported": {"source": "browser"}},
            "params": {
                "title": title.strip(),
                "description": description,
                "source": token.source,
                "source_ref": form["source_ref"],
                "on_behalf_of": record["label"],
                "media": media,
            },
        }
        raw_envelope = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )

        def protected_work(write):
            with _link_lock(
                state.root, record["id"], timeout=state.config.limits.lock_timeout_seconds
            ):
                current, live = _live_record_and_token(state.root, secret, state.tokens)
                if (
                    current is None
                    or live is None
                    or current["id"] != record["id"]
                    or live.id != token.id
                ):
                    raise _LinkUnavailable
                with admin.admin_lock(state.root):
                    preserved_hashes = _other_source_stage_hashes_under_admin_lock(
                        state.root,
                        record["id"],
                        form["source_ref"],
                        _submitted_hashes(media),
                    )
                with project.locked():
                    with project.issue_media.preserve_staged_hashes(preserved_hashes):
                        project.admit()
                        outcome = project.run_write(write)
                result_data = outcome.result_data
                receipt = result_data.get("value") if isinstance(result_data, dict) else None
                if isinstance(receipt, dict) and receipt.get("deduplicated") is False:
                    with admin.admin_lock(state.root):
                        unreferenced = _forget_source_stages_under_admin_lock(
                            state.root, record["id"], form["source_ref"]
                        )
                    if unreferenced:
                        project.issue_media.cleanup_token_stages(live.id, unreferenced)
                return outcome

        try:
            outcome, _write, _parsed = await app_module._run_authenticated_operation(
                request,
                state,
                token,
                project,
                "issue.file",
                raw_envelope,
                work=protected_work,
            )
        except _LinkUnavailable:
            return _not_found(request)
        except OpError as exc:
            raise _plain_error(exc) from None
        result = outcome.result_data
        receipt = result.get("value") if isinstance(result, dict) else None
        if not isinstance(receipt, dict) or set(receipt) != set(RECEIPT_FIELDS):
            raise _plain_error(OpError("INTEGRITY_ERROR", "issue filing receipt is invalid"))
        return app_module.AsciiJSONResponse(
            {key: receipt[key] for key in RECEIPT_FIELDS}, status_code=200
        )
    except OpError as exc:
        # A successful source/ref dedupe keeps retry media owned until its
        # ordinary expiry; terminal errors release only this source ref's stages.
        if source_ref is not None and not _retryable_link_error(exc.code):
            try:
                await _cleanup_source_stages(state, secret, record, token, project, source_ref)
            except OpError as cleanup_error:
                return app_module.envelope_error(_plain_error(cleanup_error))
        return app_module.envelope_error(_plain_error(exc))
    finally:
        if entered:
            state.limits.leave(token.id)
