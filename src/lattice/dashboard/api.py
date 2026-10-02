"""The dashboard's API, free of any HTTP transport (SPEC §10).

Reads: :func:`route_get` answers every ``GET /api/*`` path from a board's
``.lattice/`` directory and returns an :class:`ApiResponse` (status, envelope,
headers). Writes: :func:`translate_post` maps a ``POST /api/*`` to the named
operation the CLI runs for the same change, and :func:`render_write` shapes the
operation's result into the response the page has always received. The local
dashboard (``dashboard/server.py``) and the hosted one (H-13b) share both, so
a dashboard write obeys exactly the CLI's rules wherever it is served.

Nothing here writes a board: writes run through ``board.execute``.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from lattice.core.comments import materialize_comments
from lattice.core.config import get_project_type, issues_enabled
from lattice.core.errors import HTTP_STATUS, OpError
from lattice.core.ids import validate_id
from lattice.core.origin import format_origin_line, origin_matches
from lattice.core.tasks import compact_snapshot, get_artifact_evidence_refs
from lattice.core.visibility import visible
from lattice.storage.operations import (
    AuthoritativeLogError,
    discover_task_authorities,
    read_task_authority,
    resolve_task_prose_path,
)

#: The actor a local dashboard write uses when the request names none.
DEFAULT_ACTOR = "dashboard:web"

#: Maximum allowed request body size (1 MiB), to refuse oversized payloads.
MAX_REQUEST_BODY_BYTES = 1_048_576

#: Refusals of a status change that ``lattice status --force --reason`` overrides.
FORCEABLE_CODES = frozenset(
    {"INVALID_TRANSITION", "PLAN_REQUIRED", "COMPLETION_BLOCKED", "REVIEW_CYCLE_LIMIT"}
)


#: A bound checkout's client-side codes (SPEC §3.1 "CLI only"), as the
#: dashboard answers them.
CLIENT_ERROR_STATUS = {
    "SERVER_UNREACHABLE": 503,
    "OUTCOME_UNKNOWN": 503,
    "PROXY_REJECTED": 502,
    "CACHE_INCOMPLETE": 503,
}


# ---------------------------------------------------------------------------
# Envelopes and responses
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """A request the API refuses before (or instead of) running an operation."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    @classmethod
    def from_op_error(cls, exc: OpError) -> ApiError:
        """An operation's rejection, at the HTTP status SPEC §3.1 maps its code to;
        a bound checkout's client-side failures (the server unreachable, a
        write's outcome unknown, the cache unreadable) are 503."""
        status = HTTP_STATUS.get(exc.code) or CLIENT_ERROR_STATUS.get(exc.code, 500)
        return cls(status, exc.code, exc.message, exc.details)

    def envelope(self) -> dict:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


@dataclass
class ApiResponse:
    """``envelope`` is ``None`` only for a ``304 Not Modified``."""

    status: int
    envelope: dict | None
    headers: dict[str, str] = field(default_factory=dict)

    def body(self) -> bytes:
        if self.envelope is None:
            return b""
        return (json.dumps(self.envelope, sort_keys=True, indent=2) + "\n").encode("utf-8")


def ok(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> ApiResponse:
    return ApiResponse(status, {"ok": True, "data": data}, headers or {})


def error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(status, ApiError(status, code, message).envelope())


def _etagged(data: Any, etag_value: str, if_none_match: str | None, **headers: str) -> ApiResponse:
    digest = hashlib.sha256(etag_value.encode("utf-8", errors="surrogatepass")).hexdigest()
    etag = f'"{digest}"'  # ETags must be quoted per RFC 7232
    if if_none_match and if_none_match == etag:
        return ApiResponse(304, None, {"ETag": etag})
    return ok(data, headers={"ETag": etag, **headers})


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _read_config(ld: Path) -> dict:
    try:
        return json.loads((ld / "config.json").read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ApiError(500, "READ_ERROR", f"Failed to read config: {exc}") from exc


def _require_task_id(task_id: str) -> None:
    if not validate_id(task_id, "task"):
        raise ApiError(400, "INVALID_ID", "Invalid task ID format")


def with_origin_lines(events: list[dict]) -> list[dict]:
    """*events*, each with the ``origin_line`` ``show --events`` prints (AC-38).

    Events written before origins existed get none.
    """
    out = []
    for event in events:
        line = format_origin_line(event)
        out.append({**event, "origin_line": line} if line is not None else event)
    return out


def read_artifact_info(ld: Path, snapshot: dict) -> list[dict]:
    artifacts: list[dict] = []
    # Read from evidence_refs (new) with fallback to artifact_refs (legacy)
    for ref in get_artifact_evidence_refs(snapshot):
        info: dict = dict(ref)
        meta_path = ld / "artifacts" / "meta" / f"{ref['id']}.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
                info["title"] = meta.get("title")
                info["type"] = meta.get("type")
            except (json.JSONDecodeError, OSError):
                pass
        artifacts.append(info)
    return artifacts


def _board_row(snap: dict) -> dict:
    compact = compact_snapshot(snap)
    compact["updated_at"] = snap.get("updated_at")
    compact["created_at"] = snap.get("created_at")
    compact["done_at"] = snap.get("done_at")
    return compact


def get_config(ld: Path) -> dict:
    return _read_config(ld)


# ---------------------------------------------------------------------------
# Issues (LAT-365)
# ---------------------------------------------------------------------------


def _flatten_issue_comments(comments: list[dict]) -> list[dict]:
    """Flatten the task-comment-shaped thread for actor filtering and counts."""
    flattened = []
    pending = list(reversed(comments))
    while pending:
        comment = pending.pop()
        flattened.append(comment)
        replies = comment.get("replies", [])
        if isinstance(replies, list):
            pending.extend(reversed(replies))
    return flattened


def _issue_detail_adapter(ld: Path, raw_id: str) -> dict | None:
    """The page's issue detail: LAT-371's ``issue_detail`` plus media URLs."""
    from lattice.storage.issues import issue_detail

    try:
        detail = issue_detail(ld, raw_id)
    except OpError as exc:
        raise ApiError.from_op_error(exc) from exc
    return None if detail is None else _normalize_issue_detail(detail)


def _normalize_issue_detail(detail: dict) -> dict:
    """Give media entries dashboard URLs and hide board-local file paths."""
    from lattice.core.issue_media import parse_frame_name

    issue = dict(detail)
    issue.setdefault("comments", [])
    issue.setdefault("events", [])
    media = []
    for entry in issue.get("media", []):
        item = dict(entry)
        media_id = item.get("id")
        issue_id = issue.get("id")
        valid_issue_id = isinstance(issue_id, str) and validate_id(issue_id, "iss")
        valid_media_id = isinstance(media_id, str) and validate_id(media_id, "med")
        item["url"] = (
            f"/api/issues/{issue_id}/media/{media_id}"
            if valid_issue_id
            and valid_media_id
            and item.get("path")
            and not item.get("missing")
            and not item.get("removed")
            else None
        )
        frames = []
        for frame in item.get("frames", []):
            frame_item = dict(frame)
            raw_path = frame_item.get("path")
            frame_name = Path(raw_path).name if isinstance(raw_path, str) else ""
            frame_item["url"] = (
                f"/api/issues/{issue_id}/media/{media_id}/frames/{frame_name}"
                if valid_issue_id
                and valid_media_id
                and parse_frame_name(frame_name) is not None
                and not item.get("removed")
                else None
            )
            frames.append(frame_item)
        item["frames"] = frames
        item.pop("path", None)
        for frame in item["frames"]:
            frame.pop("path", None)
        media.append(item)
    issue["media"] = media
    return issue


def _issue_list_adapter(ld: Path, actor: str | None = None) -> list[dict]:
    """Every issue, or with *actor* those it filed or commented on (any state).

    Membership is LAT-371's ``issues_by``: a full key (``human:Atin-1``) matches
    that session exactly, a legacy key (``human:Atin``) spans the person's
    sessions. Each matched row adds the actor's own comment stats for the person
    page: ``matched_by``, ``actor_comment_count``, ``actor_comment_origins`` and
    ``actor_activity_at``.
    """
    from lattice.core.issues import actor_matches, issue_comments
    from lattice.storage.issues import (
        issue_views,
        issues_by,
        list_issue_snapshots,
        read_issue_events,
    )

    if actor is None:
        views = issue_views(ld, list_issue_snapshots(ld))
        return [_normalize_issue_detail(view) for view in views]
    rows = []
    for view in issues_by(ld, actor):
        comments = _flatten_issue_comments(issue_comments(read_issue_events(ld, view["id"])))
        mine = [c for c in comments if actor_matches(c.get("author"), actor)]
        times = [c.get("created_at") or "" for c in mine]
        if view["activity"] == "filed":
            times.append(view.get("filed_at") or "")
        rows.append(
            {
                **_normalize_issue_detail(view),
                "matched_by": view["activity"],
                "actor_comment_count": len(mine),
                "actor_comment_origins": [c.get("origin") for c in mine],
                "actor_activity_at": max(times, default=""),
            }
        )
    return rows


def _require_issues_enabled(ld: Path) -> None:
    if not issues_enabled(_read_config(ld)):
        raise ApiError(409, "ISSUES_DISABLED", "The issue log is disabled on this board.")


#: ``/api/tasks`` query parameters that filter by origin, as ``lattice list``
#: names them (``--machine``, ``--user``, ``--worktree``), with the longest
#: value each accepts.
ORIGIN_FILTER_LIMITS = {"machine": 256, "user": 256, "worktree": 1024}


def origin_filter_params(query: dict[str, list[str]]) -> dict[str, str]:
    """The origin filters of a ``/api/tasks`` query, validated and normalized.

    An empty value is no filter. The worktree must be absolute and is
    normalized lexically as the CLI's ``--worktree`` is (``abspath``, then
    ``resolve`` also folds a leading ``//``), so a trailing slash, repeated
    slash or ``.``/``..`` segment matches as in the CLI. A relative
    path, ``~``, or a symlink alias is the CLI's client-side resolution and
    cannot be done here: a server never resolves a caller's path against its
    own filesystem, so a relative worktree is refused and a symlink alias
    matches nothing.
    """
    params: dict[str, str] = {}
    for key, limit in ORIGIN_FILTER_LIMITS.items():
        values = query.get(key)
        if not values:
            continue
        value = values[0]
        if len(value) > limit:
            raise ApiError(
                400, "VALIDATION_ERROR", f"{key} filter is longer than {limit} characters"
            )
        if key == "worktree":
            if not value.startswith("/"):
                raise ApiError(
                    400, "VALIDATION_ERROR", f"worktree filter must be an absolute path: '{value}'"
                )
            value = "/" + posixpath.normpath(value).lstrip("/")
        params[key] = value
    return params


def get_tasks(
    ld: Path,
    *,
    machine: str | None = None,
    user: str | None = None,
    worktree: str | None = None,
) -> list[dict]:
    """Active tasks for the boards; erased tasks are left out (SPEC §7).

    *machine*, *user* and *worktree* filter as ``lattice list`` does: a task
    matches when one event in its log carries an origin satisfying every one
    given (:func:`origin_matches`), so a task written before v2 never matches.
    The worktree is matched as given; :func:`origin_filter_params` normalizes
    a query's.
    """
    authorities = discover_task_authorities(ld, include_archived=False)
    if machine is not None or user is not None or worktree is not None:
        worktrees = frozenset({worktree}) if worktree is not None else None
        authorities = [
            a
            for a in authorities
            if any(
                origin_matches(event, user=user, machine=machine, worktrees=worktrees)
                for event in a.events
            )
        ]
    snapshots = visible(a.snapshot for a in authorities)
    rows = []
    for snap in snapshots:
        row = _board_row(snap)
        row["has_active_session"] = bool(
            snap.get("status") == "in_progress" and snap.get("assigned_to")
        )
        rows.append(row)
    rows.sort(key=lambda s: s.get("id", ""))
    return rows


def get_archived(ld: Path) -> list[dict]:
    snapshots = visible(
        a.snapshot
        for a in discover_task_authorities(ld, include_archived=True)
        if a.location == "archived"
    )
    rows = []
    for snap in snapshots:
        row = _board_row(snap)
        row["archived"] = True
        rows.append(row)
    rows.sort(key=lambda s: s.get("id", ""))
    return rows


def _task_detail(ld: Path, task_id: str) -> tuple[dict, Any]:
    _require_task_id(task_id)
    authority = read_task_authority(ld, task_id, allow_missing=True)
    if authority is None:
        raise ApiError(404, "NOT_FOUND", f"Task {task_id} not found")
    snapshot = authority.snapshot
    notes_path, _ = resolve_task_prose_path(ld, task_id, "notes")
    plan_path, _ = resolve_task_prose_path(ld, task_id, "plan")
    result = dict(snapshot)
    result["notes_exists"] = notes_path is not None
    result["plan_exists"] = plan_path is not None
    result["artifacts"] = read_artifact_info(ld, snapshot)
    result["has_active_session"] = bool(
        snapshot.get("status") == "in_progress" and snapshot.get("assigned_to")
    )
    if authority.location == "archived":
        result["archived"] = True
    return result, authority


def get_task_detail(ld: Path, task_id: str) -> dict:
    """One task, erased or not (the page shows an erased task's reason)."""
    return _task_detail(ld, task_id)[0]


def get_task_events(ld: Path, task_id: str) -> list[dict]:
    _require_task_id(task_id)
    try:
        authority = read_task_authority(ld, task_id, allow_missing=True)
    except AuthoritativeLogError as exc:
        raise ApiError(409, "INTEGRITY_ERROR", str(exc)) from exc
    events = list(authority.events) if authority is not None else []
    events.reverse()  # newest first
    return with_origin_lines(events)


def get_task_comments(ld: Path, task_id: str) -> list[dict]:
    _require_task_id(task_id)
    authority = read_task_authority(ld, task_id, allow_missing=True)
    events = list(authority.events) if authority is not None else []
    return materialize_comments(events)


def get_task_full(ld: Path, task_id: str) -> dict:
    """Snapshot, the latest 20 events, and the comment tree (Cube LOD 4)."""
    result, authority = _task_detail(ld, task_id)
    events = list(authority.events)
    result["recent_events"] = with_origin_lines(list(reversed(events))[:20])
    result["comments"] = materialize_comments(events)
    return result


def get_stats(ld: Path) -> dict:
    from lattice.core.stats import build_stats

    return build_stats(ld, _read_config(ld), include_tombstoned=False)


def get_graph(ld: Path, if_none_match: str | None = None) -> ApiResponse:
    """Nodes and directed edges for the graph views, with a revision ETag."""
    snapshots = visible(a.snapshot for a in discover_task_authorities(ld, include_archived=False))
    active_ids: set[str] = {s["id"] for s in snapshots if "id" in s}
    nodes: list[dict] = []
    for snap in snapshots:
        nodes.append(
            {
                "id": snap.get("id"),
                "short_id": snap.get("short_id"),
                "title": snap.get("title"),
                "status": snap.get("status"),
                "priority": snap.get("priority"),
                "type": snap.get("type"),
                "assigned_to": snap.get("assigned_to"),
                "needs_human": snap.get("needs_human"),
                "branch_links": snap.get("branch_links", []),
                "created_at": snap.get("created_at"),
                "updated_at": snap.get("updated_at"),
                "description_snippet": (snap.get("description") or "")[:200],
            }
        )
    # Edges are directed: source is the task holding the relationship, target
    # the referenced task (only when the target is on the board).
    links: list[dict] = []
    for snap in snapshots:
        for rel in snap.get("relationships_out", []):
            target_id = rel.get("target_task_id")
            if target_id and target_id in active_ids:
                links.append(
                    {"source": snap.get("id"), "target": target_id, "type": rel.get("type")}
                )

    projection = json.dumps(
        {"nodes": nodes, "links": links}, sort_keys=True, separators=(",", ":")
    )
    revision = hashlib.sha256(projection.encode("utf-8")).hexdigest()
    return _etagged(
        {"nodes": nodes, "links": links, "revision": revision}, revision, if_none_match
    )


def get_activity(ld: Path, query: dict[str, list[str]]) -> dict:
    """``/api/activity``: newest-first events, paginated and filtered, with facets."""

    def _qs(key: str) -> str | None:
        vals = query.get(key)
        return vals[0] if vals else None

    try:
        limit = max(1, min(200, int(_qs("limit") or "50")))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(0, int(_qs("offset") or "0"))
    except (ValueError, TypeError):
        offset = 0

    type_filter = _qs("type")
    task_param = _qs("task")
    actor_filter = _qs("actor")
    after = _qs("after")
    before = _qs("before")
    search = _qs("search")
    has_filters = any([type_filter, task_param, actor_filter, after, before, search])

    empty = {
        "events": [],
        "total": 0,
        "offset": offset,
        "limit": limit,
        "has_more": False,
        "facets": {"types": [], "actors": [], "tasks": []},
    }
    task_filter: str | None = None
    if task_param:
        if validate_id(task_param, "task"):
            task_filter = task_param
        else:
            from lattice.core.ids import is_short_id
            from lattice.storage.short_ids import resolve_short_id

            if not is_short_id(task_param):
                raise ApiError(400, "VALIDATION_ERROR", f"Invalid task filter: '{task_param}'")
            task_filter = resolve_short_id(ld, task_param.upper())
            if task_filter is None:
                return empty  # unknown short ID

    # Full scan when filters are active, tail otherwise.
    all_events = collect_events(ld, full_scan=has_filters, tail_n=10)
    facets = build_facets(all_events, ld)
    filtered = sort_activity_newest_first(
        apply_activity_filters(
            all_events,
            type_filter=type_filter,
            task_filter=task_filter,
            actor_filter=actor_filter,
            after=after,
            before=before,
            search=search,
        )
    )
    total = len(filtered)
    return {
        "events": with_origin_lines(filtered[offset : offset + limit]),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
        "facets": facets,
    }


def _structure_gate(ld: Path) -> None:
    if get_project_type(_read_config(ld)) != "structure":
        raise ApiError(
            403,
            "NOT_STRUCTURE_PROJECT",
            "Structure endpoints are only available on structure projects.",
        )


def _load_structure_file(project_root: Path) -> tuple[Any, str | None]:
    """``structure.json`` (or ``structure.yaml`` via a lazy pyyaml import) at the project root."""
    json_path = project_root / "structure.json"
    if json_path.is_file():
        try:
            return json.loads(json_path.read_text()), None
        except (json.JSONDecodeError, OSError) as exc:
            return None, f"Failed to parse structure.json: {exc}"
    yaml_path = project_root / "structure.yaml"
    if yaml_path.is_file():
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            return None, (
                "structure.yaml found but pyyaml is not installed. "
                "Install pyyaml or provide structure.json instead."
            )
        try:
            return yaml.safe_load(yaml_path.read_text()), None
        except (OSError, Exception) as exc:  # noqa: BLE001
            return None, f"Failed to parse structure.yaml: {exc}"
    return None, "structure.json / structure.yaml not found at project root."


def get_structure(ld: Path) -> Any:
    _structure_gate(ld)
    data, err = _load_structure_file(ld.parent)
    if err is not None:
        raise ApiError(404, "STRUCTURE_NOT_FOUND", err)
    return data


def get_structure_events(ld: Path, query: dict[str, list[str]]) -> dict:
    """The tail of ``events.jsonl`` at a structure project's root."""
    _structure_gate(ld)
    try:
        limit = int(query.get("limit", ["200"])[0])
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 2000))
    events_path = ld.parent / "events.jsonl"
    if not events_path.is_file():
        return {"events": [], "truncated": False}
    try:
        raw_lines = events_path.read_text().splitlines()
    except OSError as exc:
        raise ApiError(500, "READ_ERROR", f"Failed to read events.jsonl: {exc}") from exc
    events: list[dict] = []
    for line in raw_lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # event logs may be mid-write
    return {"events": events, "truncated": len(raw_lines) > limit}


def get_git_summary(ld: Path, if_none_match: str | None = None) -> ApiResponse:
    from lattice.dashboard.git_reader import get_git_summary as _summary

    summary, etag_value = _summary(ld)
    if not summary.get("available", False) or not etag_value:
        return ok(summary)
    return _etagged(summary, etag_value, if_none_match, **{"Cache-Control": "max-age=30"})


def get_git_branch_commits(ld: Path, branch_name: str) -> dict:
    from lattice.dashboard.git_reader import (
        _validate_branch_name,
        find_git_root,
        get_recent_commits,
        git_available,
    )

    if not branch_name:
        raise ApiError(400, "VALIDATION_ERROR", "Branch name is required")
    branch_name = unquote(branch_name)  # e.g. %2F -> /
    if not _validate_branch_name(branch_name):  # nothing a git flag could be read from
        raise ApiError(400, "VALIDATION_ERROR", "Invalid branch name")
    if not git_available():
        return {"available": False, "reason": "git_not_installed"}
    repo_root = find_git_root(ld.parent)
    if repo_root is None:
        return {"available": False, "reason": "not_a_git_repo"}
    commits = get_recent_commits(repo_root, branch_name)
    return {"branch": branch_name, "commits": commits, "count": len(commits)}


def route_get(
    ld: Path, path: str, query_string: str = "", if_none_match: str | None = None
) -> ApiResponse:
    """Answer ``GET <path>`` (an ``/api/...`` path, no trailing slash)."""
    query = parse_qs(query_string)
    try:
        if path == "/api/config":
            return ok(get_config(ld))
        if path == "/api/issues":
            _require_issues_enabled(ld)
            values = query.get("by")
            actor = values[0] if values else None
            if actor == "":
                actor = None
            if actor is not None and len(actor) > 256:
                raise ApiError(400, "VALIDATION_ERROR", "by filter is longer than 256 characters")
            try:
                rows = _issue_list_adapter(ld, actor)
            except OpError as exc:
                raise ApiError.from_op_error(exc) from exc
            # List rows intentionally carry no comments or history; detail has
            # one endpoint and keeps event redaction in the LAT-371 reader.
            for row in rows:
                row.pop("comments", None)
                row.pop("events", None)
            return ok(rows)
        if path.startswith("/api/issues/"):
            _require_issues_enabled(ld)
            remainder = path[len("/api/issues/") :]
            if "/" in remainder:
                return error(404, "NOT_FOUND", f"Not found: {path}")
            try:
                detail = _issue_detail_adapter(ld, remainder)
            except OpError as exc:
                raise ApiError.from_op_error(exc) from exc
            if detail is None:
                return error(404, "NOT_FOUND", f"No issue {remainder}.")
            return ok(detail)
        if path == "/api/tasks":
            return ok(get_tasks(ld, **origin_filter_params(query)))
        if path == "/api/stats":
            return ok(get_stats(ld))
        if path == "/api/activity":
            return ok(get_activity(ld, query))
        if path == "/api/archived":
            return ok(get_archived(ld))
        if path == "/api/graph":
            return get_graph(ld, if_none_match)
        if path == "/api/structure":
            return ok(get_structure(ld))
        if path == "/api/structure/events":
            return ok(get_structure_events(ld, query))
        if path == "/api/git":
            return get_git_summary(ld, if_none_match)
        if path.startswith("/api/git/branches/"):
            remainder = path[len("/api/git/branches/") :]
            if remainder.endswith("/commits"):
                return ok(get_git_branch_commits(ld, remainder[: -len("/commits")]))
            return error(404, "NOT_FOUND", f"Not found: {path}")
        if path.startswith("/api/tasks/"):
            remainder = path[len("/api/tasks/") :]
            if "/" not in remainder:
                return ok(get_task_detail(ld, remainder))
            task_id, sub = remainder.rsplit("/", 1)
            readers: dict[str, Callable[[Path, str], Any]] = {
                "events": get_task_events,
                "comments": get_task_comments,
                "full": get_task_full,
            }
            if sub in readers:
                return ok(readers[sub](ld, task_id))
            return error(404, "NOT_FOUND", f"Not found: {path}")
    except ApiError as exc:
        return ApiResponse(exc.status, exc.envelope())
    return error(404, "NOT_FOUND", f"Unknown API endpoint: {path}")


# ---------------------------------------------------------------------------
# Activity helpers
# ---------------------------------------------------------------------------


def collect_events(ld: Path, *, full_scan: bool = False, tail_n: int = 10) -> list[dict]:
    """Every task's events (``full_scan``, archived included) or each log's last *tail_n*."""
    return [
        event
        for authority in discover_task_authorities(ld, include_archived=full_scan)
        for event in (authority.events if full_scan else authority.events[-tail_n:])
    ]


def sort_activity_newest_first(events: list[dict]) -> list[dict]:
    """Return *events* newest first, keeping each task's log order on ties.

    ``ts`` has one-second precision, so events written in the same second tie.
    The event ``id`` is not a safe tiebreak inside one task: ULIDs are not
    guaranteed monotonic (python-ulid reads the clock twice per ID and can
    emit an older-looking ID within one process), so ``task_archived`` could
    sort above the ``task_unarchived`` that followed it. A task's event log
    is the true order, so within each same-second group of one task, the
    group's IDs are handed out in log order and used as the tiebreak. Ties
    across tasks still fall to the event ID.

    *events* must list each task's events in log order, as
    :func:`collect_events` and :func:`apply_activity_filters` leave them.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for index, event in enumerate(events):
        groups.setdefault((event.get("task_id", ""), event.get("ts", "")), []).append(index)
    tiebreak = [""] * len(events)
    for indices in groups.values():
        for index, event_id in zip(indices, sorted(events[i].get("id", "") for i in indices)):
            tiebreak[index] = event_id
    order = sorted(
        range(len(events)),
        key=lambda i: (events[i].get("ts", ""), tiebreak[i]),
        reverse=True,
    )
    return [events[i] for i in order]


def build_facets(events: list[dict], ld: Path) -> dict:
    """Distinct types, actors, and tasks of *events*, for the filter dropdowns."""
    from lattice.core.events import get_actor_display

    types: set[str] = set()
    actors: set[str] = set()
    task_ids: set[str] = set()
    for ev in events:
        if ev.get("type"):
            types.add(ev["type"])
        if ev.get("actor"):
            actors.add(get_actor_display(ev["actor"]))
        if ev.get("task_id"):
            task_ids.add(ev["task_id"])

    task_info: list[dict] = []
    for tid in sorted(task_ids):
        info: dict = {"id": tid}
        authority = read_task_authority(ld, tid, allow_missing=True)
        if authority is not None:
            info["short_id"] = authority.snapshot.get("short_id")
            info["title"] = authority.snapshot.get("title")
        task_info.append(info)
    return {"types": sorted(types), "actors": sorted(actors), "tasks": task_info}


def apply_activity_filters(
    events: list[dict],
    *,
    type_filter: str | None = None,
    task_filter: str | None = None,
    actor_filter: str | None = None,
    after: str | None = None,
    before: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """Apply the filter chain to *events*; all filters are AND-combined."""
    from lattice.core.events import get_actor_display

    result = events
    if type_filter:
        allowed = {t.strip() for t in type_filter.split(",")}
        result = [e for e in result if e.get("type") in allowed]
    if task_filter:
        result = [e for e in result if e.get("task_id") == task_filter]
    if actor_filter:
        result = [
            e for e in result if e.get("actor") and get_actor_display(e["actor"]) == actor_filter
        ]
    if after:
        result = [e for e in result if (e.get("ts") or "") > after]
    if before:
        result = [e for e in result if (e.get("ts") or "") < before]
    if search:
        needle = search.lower()

        def _matches(ev: dict) -> bool:
            # Event data values (comment bodies, field values), then actor and type.
            for v in (ev.get("data") or {}).values():
                if isinstance(v, str) and needle in v.lower():
                    return True
            actor_str = get_actor_display(ev["actor"]) if ev.get("actor") else ""
            return needle in actor_str.lower() or needle in (ev.get("type") or "").lower()

        result = [e for e in result if _matches(e)]
    return result


# ---------------------------------------------------------------------------
# Writes: POST -> operation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteRequest:
    """One dashboard POST as the operation it runs.

    ``actor``: the actor the request body named, or ``None`` (the caller then
    supplies the default: ``dashboard:web`` locally, the browser actor on a
    bound checkout, SPEC §8.3). ``render(result) -> (status, data)`` builds the
    response the page expects from the ``OpResult``.
    """

    op_name: str
    params: dict
    actor: Any
    render: Callable[[Any], tuple[int, Any]]
    task_id: str | None = None
    new_status: str | None = None


def _invalid(message: str) -> ApiError:
    return ApiError(400, "VALIDATION_ERROR", message)


def _snapshot(result: Any) -> tuple[int, Any]:
    return 200, result.task


def _require_str(body: dict, key: str, message: str) -> str:
    value = body.get(key)
    if not value or not isinstance(value, str):
        raise _invalid(message)
    return value


def _optional(body: dict, *keys: str) -> dict:
    """The *keys* the body sets to something other than ``null``."""
    return {k: body[k] for k in keys if body.get(k) is not None}


def _update_pairs(fields: Any) -> tuple[str, ...]:
    """``lattice update``'s ``field=value`` pairs for the page's ``fields`` object."""
    if not fields or not isinstance(fields, dict):
        raise _invalid("Missing or invalid 'fields' object")
    if "title" in fields and (not isinstance(fields["title"], str) or not fields["title"].strip()):
        raise _invalid("Title must be a non-empty string")
    pairs = []
    for name, value in fields.items():
        if name == "tags":
            if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
                raise _invalid("'tags' must be an array of strings")
            value = ",".join(value)
        elif not isinstance(value, str):
            raise _invalid(f"'{name}' must be a string")
        pairs.append(f"{name}={value}")
    return tuple(pairs)


def _task_write(task_id: str, sub: str, body: dict) -> WriteRequest:
    actor = body.get("actor")
    base = {"task": task_id}

    if sub == "status":
        status = body.get("status")
        if not status:
            raise _invalid("Missing 'status' field")
        if not isinstance(status, str):
            raise _invalid("'status' must be a string")

        def render_status(result: Any) -> tuple[int, Any]:
            if result.idempotent:
                return 200, {"message": f"Already at status {status}"}
            return 200, result.task

        params = {**base, "new_status": status, **_optional(body, "force", "reason")}
        return WriteRequest("task.status", params, actor, render_status, task_id, status)

    if sub == "assign":
        assigned_to = body.get("assigned_to")
        return WriteRequest(
            "task.assign",
            {**base, "actor_id": "none" if assigned_to is None else assigned_to},
            actor,
            _snapshot,
            task_id,
        )

    if sub == "comment":
        params = {**base, "text": body.get("body", "")}
        if body.get("parent_id") is not None:
            params["reply_to"] = body["parent_id"]
        return WriteRequest("task.comment", params, actor, _snapshot, task_id)

    if sub == "update":
        params = {**base, "pairs": _update_pairs(body.get("fields"))}
        return WriteRequest("task.update", params, actor, _snapshot, task_id)

    if sub == "archive":
        return WriteRequest(
            "task.archive",
            base,
            actor,
            lambda _result: (200, {"message": f"Task {task_id} archived"}),
            task_id,
        )

    if sub in ("comment-edit", "comment-delete", "react", "unreact"):
        comment_id = _require_str(body, "comment_id", "Missing or invalid 'comment_id' field")
        params = {**base, "comment_id": comment_id}
        if sub == "comment-edit":
            params["new_text"] = body.get("body", "")
            params.update(_optional(body, "role"))
            if body.get("clear_role", False) is not False:
                params["clear_role"] = body["clear_role"]
            return WriteRequest("task.comment_edit", params, actor, _snapshot, task_id)
        if sub == "comment-delete":
            return WriteRequest("task.comment_delete", params, actor, _snapshot, task_id)
        params["emoji"] = _require_str(body, "emoji", "Missing or invalid 'emoji' field")
        return WriteRequest(f"task.{sub}", params, actor, _snapshot, task_id)

    raise ApiError(404, "NOT_FOUND", f"Not found: /api/tasks/{task_id}/{sub}")


def translate_post(path: str, body: Any) -> WriteRequest:
    """The operation ``POST <path>`` with JSON *body* runs.

    Raises :class:`ApiError` for an unknown path (404) or a body the page
    could never have sent (400); every rule about the change itself is the
    operation's, with the CLI's codes and messages (SPEC §10, G-6).
    """
    if (
        path == "/api/tasks"
        or path == "/api/config/dashboard"
        or path.startswith("/api/tasks/")
        or path == "/api/issues"
        or path.startswith("/api/issues/")
    ):
        if not isinstance(body, dict):
            raise _invalid("Request body must be a JSON object")
    if path == "/api/config/dashboard":
        settings = {k: v for k, v in body.items() if k != "actor"}
        return WriteRequest(
            "board.set_dashboard_config",
            {"settings": settings},
            body.get("actor"),
            lambda result: (200, result.value),
        )
    if path == "/api/tasks":
        title = body.get("title")
        if not title or not isinstance(title, str) or not title.strip():
            raise _invalid("Missing or empty 'title' field")
        tags = body.get("tags")
        if tags is not None and (
            not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)
        ):
            raise _invalid("'tags' must be an array of strings")
        params = {
            "title": title.strip(),
            **_optional(body, "status", "priority", "type", "description", "urgency"),
            **_optional(body, "assigned_to"),
        }
        if tags:
            params["tag"] = tuple(tags)
        return WriteRequest("task.create", params, body.get("actor"), lambda r: (201, r.value))
    if path.startswith("/api/tasks/"):
        remainder = path[len("/api/tasks/") :]
        if "/" in remainder:
            task_id, sub = remainder.rsplit("/", 1)
            _require_task_id(task_id)
            return _task_write(task_id, sub, body)
        raise ApiError(404, "NOT_FOUND", f"Not found: {path}")
    if path == "/api/issues":
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            raise _invalid("Missing or empty 'title' field")
        description = body.get("description", "")
        if not isinstance(description, str):
            raise _invalid("'description' must be a string")
        media = body.get("media", [])
        if not isinstance(media, list) or not all(isinstance(item, dict) for item in media):
            raise _invalid("'media' must be an array of objects")

        params = {"title": title, "description": description, "media": tuple(media)}

        def render_issue(result: Any) -> tuple[int, Any]:
            value = dict(result.value)
            value["events"] = result.events
            return 201, _normalize_issue_detail(value)

        return WriteRequest("issue.file", params, body.get("actor"), render_issue)
    if path.startswith("/api/issues/"):
        remainder = path[len("/api/issues/") :]
        issue_id, separator, sub = remainder.partition("/")
        if not separator or sub != "comment":
            raise ApiError(404, "NOT_FOUND", f"Not found: {path}")
        _require_issue_ref(issue_id)
        if body.get("parent_id") is not None:
            raise _invalid("The dashboard posts top-level comments only")
        text = body.get("body")
        if not isinstance(text, str) or not text.strip():
            raise _invalid("Missing or empty 'body' field")
        params = {"issue": issue_id, "text": text}
        return WriteRequest(
            "issue.comment", params, body.get("actor"), lambda result: (200, result.value)
        )
    raise ApiError(404, "NOT_FOUND", f"Unknown API endpoint: {path}")


def _require_issue_ref(issue_id: str) -> None:
    """Validate issue path IDs with the storage resolver's shared grammar."""
    from lattice.core.issues import parse_issue_ref

    if parse_issue_ref(issue_id) is None:
        raise ApiError(400, "INVALID_ID", f"Invalid issue ID format: '{issue_id}'.")


def write_error(request: WriteRequest, exc: OpError) -> ApiError:
    """An operation's rejection of a dashboard write, as the page shows it.

    A refused status change the CLI can override names the override, since
    the dashboard has no force control (SPEC §10).
    """
    refused = ApiError.from_op_error(exc)
    if (
        request.op_name == "task.status"
        and exc.code in FORCEABLE_CODES
        and exc.details.get("reason") != "STALE_ATTESTATION"
    ):
        snapshot = exc.details.get("snapshot") or {}
        task = snapshot.get("short_id") or request.task_id
        refused.message = (
            f"{exc.message} From the CLI: "
            f'lattice status {task} {request.new_status} --force --reason "..."'
        )
    return refused


# ---------------------------------------------------------------------------
# The browser actor (SPEC §8.3)
# ---------------------------------------------------------------------------


def browser_actor(identity: dict) -> str:
    """The actor a dashboard write on a bound checkout acts as.

    *identity* is the ``identity`` object ``/v1/info`` returns for the
    checkout's token; the server computes ``browser_actor`` from the token
    (its user when one of its patterns matches it, else its default actor).
    ``MISSING_ACTOR`` when the token has neither.
    """
    actor = identity.get("browser_actor")
    if isinstance(actor, str) and actor:
        return actor
    raise OpError(
        "MISSING_ACTOR",
        "This checkout's token names no actor a browser can write as: its user is not "
        "among its permitted actors, and it has no single default actor.",
    )
