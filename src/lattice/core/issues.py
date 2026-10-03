"""The optional issue log (LAT-361): pure logic, no filesystem.

An issue is an observation someone filed ("the footer overlaps the CTA at
400px"), kept apart from tasks, which are commitments. Each issue has its own
event log; its snapshot is a replay of that log. Links to tasks are recorded on
the issue only. The state (open, linked, resolved, dismissed, duplicate) is
never stored: :func:`derive_issue_state` computes it from the snapshot and the
linked tasks' current statuses.

The display ID format lives in :func:`format_issue_short_id` and
:func:`parse_issue_ref` and nowhere else.

Issue titles and descriptions are stored separately for new issues; old
``text``-only logs keep their frozen derivation rule. Comments share the task
comment shape while remaining in each issue's own event log.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from lattice.core.comments import materialize_comments
from lattice.core.events import get_actor_display
from lattice.core.ids import validate_id
from lattice.core.issue_media import media_summary, present_media

ISSUE_STATES: tuple[str, ...] = ("open", "linked", "resolved", "dismissed", "duplicate")
#: What ``issue list`` shows without ``--state`` or ``--all``: the issues that still need a look.
DEFAULT_LIST_STATES: tuple[str, ...] = ("open", "linked")
CONFIDENCE_VALUES: tuple[str, ...] = ("possible", "definite")
CLOSURE_KINDS: tuple[str, ...] = ("dismissed", "duplicate")
TITLE_LIMIT = 120

ENABLE_LINE = '"issues": {"enabled": true}'


def issues_disabled_message(has_existing: bool) -> str:
    """The local-board ``ISSUES_DISABLED`` message and any preserved-data clause."""
    message = (
        "The issue log is off for this project. The board owner turns it on by adding "
        f"{ENABLE_LINE} to .lattice/config.json."
    )
    if has_existing:
        message += " Existing issues are kept and reappear when it is on."
    return message


def hosted_issues_disabled_message(has_existing: bool, project: str) -> str:
    """The hosted-board ``ISSUES_DISABLED`` guidance for its server-side owner."""
    message = (
        "The issue log is off for this project. The board owner turns it on by running "
        f"'lattice server project config {project} --set issues.enabled=true' on the server host."
    )
    if has_existing:
        message += " Existing issues are kept and reappear when it is on."
    return message


def unreadable_issue_warning(path: object, error: Exception) -> str:
    """The stderr line a read prints for an issue file it cannot read."""
    cause = error.__cause__ or error
    return (
        f"Warning: issue file {path} is unreadable ({cause}). "
        "Run 'lattice rebuild --all' to rebuild it from its log."
    )


def hosted_unreadable_issue_warning(path: object, error: Exception) -> str:
    """The warning for issue metadata on a hosted, read-only cache."""
    cause = error.__cause__ or error
    return (
        f"Warning: issue file {path} is unreadable ({cause}). "
        "Run 'lattice rebuild --all --offline-maintenance' on the server host "
        "to rebuild it from its log."
    )


# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------

_SEQ_REF_RE = re.compile(r"^(?:([A-Z][A-Z0-9]{0,4}(?:-[A-Z][A-Z0-9]{0,4})?)-)?I(\d+)$")


def format_issue_short_id(project_code: str | None, seq: int) -> str:
    """The display ID of issue number *seq*: ``LAT-I3``, or ``I3`` with no project code."""
    return f"{project_code}-I{seq}" if project_code else f"I{seq}"


def parse_issue_ref(raw: str) -> tuple | None:
    """Parse an issue reference, case-insensitively.

    Returns ``("ulid", "iss_<ULID>")``, ``("seq", prefix_or_None, n)``, or
    ``None`` when *raw* is none of ``iss_<ULID>``, ``I<n>``, ``<CODE>-I<n>``.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.lower().startswith("iss_"):
        candidate = "iss_" + text[4:].upper()
        return ("ulid", candidate) if validate_id(candidate, "iss") else None
    match = _SEQ_REF_RE.fullmatch(text.upper())
    if match is None:
        return None
    n = int(match.group(2))
    if n < 1:
        return None
    return ("seq", match.group(1), n)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _filed(snapshot: dict | None, event: dict) -> dict:
    if snapshot is not None:
        raise ValueError(f"issue {event.get('issue_id')} was filed twice ({event.get('id')})")
    data = event.get("data", {})
    new: dict = {
        "schema_version": 1,
        "id": event["issue_id"],
        "short_id": data.get("short_id"),
        "seq": data.get("seq"),
        "filed_by": event.get("actor"),
        "filed_at": event.get("ts"),
        "links": [],
        "closure": None,
    }
    if "title" in data:
        new["title"] = data["title"]
        new["description"] = data.get("description", "")
    else:
        new["text"] = data.get("text", "")
    for key in ("confidence", "evidence", "source"):
        if key in data:
            new[key] = data[key]
    return new


def _linked(snapshot: dict, event: dict) -> None:
    task_id = event["data"]["task_id"]
    if any(link["task_id"] == task_id for link in snapshot["links"]):
        return
    snapshot["links"].append(
        {"task_id": task_id, "linked_at": event.get("ts"), "linked_by": event.get("actor")}
    )


def _unlinked(snapshot: dict, event: dict) -> None:
    task_id = event["data"]["task_id"]
    snapshot["links"] = [link for link in snapshot["links"] if link["task_id"] != task_id]


def _dismissed(snapshot: dict, event: dict) -> None:
    snapshot["closure"] = {
        "kind": "dismissed",
        "reason": event.get("data", {}).get("reason"),
        "at": event.get("ts"),
        "by": event.get("actor"),
    }


def _marked_duplicate(snapshot: dict, event: dict) -> None:
    snapshot["closure"] = {
        "kind": "duplicate",
        "duplicate_of": event["data"]["duplicate_of"],
        "at": event.get("ts"),
        "by": event.get("actor"),
    }


def _reopened(snapshot: dict, _event: dict) -> None:
    snapshot["closure"] = None


def _edited(snapshot: dict, event: dict) -> None:
    """Apply title/description values, materializing a legacy ``text`` issue."""
    if "title" not in snapshot:
        title, description = issue_title_description(snapshot)
        snapshot["title"] = title
        snapshot["description"] = description
    data = event.get("data", {})
    for key in ("title", "description"):
        if key in data:
            snapshot[key] = data[key]
    snapshot.pop("text", None)


def _comment_added(snapshot: dict, _event: dict) -> None:
    snapshot["comment_count"] = snapshot.get("comment_count", 0) + 1


#: The ``issue_media_added`` data fields a snapshot's media entry keeps (LAT-366).
MEDIA_ENTRY_FIELDS: tuple[str, ...] = (
    "n",
    "kind",
    "content_type",
    "original_name",
    "size_bytes",
    "sha256",
    "width",
    "height",
    "duration_ms",
    "converted_from",
)
_MEDIA_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _valid_media_sha256(value: object) -> bool:
    return isinstance(value, str) and _MEDIA_SHA256_RE.fullmatch(value) is not None


def validate_issue_media_hashes(snapshot: Mapping) -> None:
    """Reject malformed persisted issue and media snapshot shapes or hashes."""
    if not isinstance(snapshot, Mapping):
        raise ValueError("issue snapshot must be an object")
    entries = snapshot.get("media", [])
    if not isinstance(entries, list):
        raise ValueError("issue media must be a list")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("issue media entry must be an object")
        if "content_type" in entry and not isinstance(entry["content_type"], str):
            raise ValueError("issue media content_type must be a string")
        if "sha256" in entry and not _valid_media_sha256(entry["sha256"]):
            raise ValueError("issue media sha256 must be 64 lowercase hexadecimal characters")
        source = entry.get("converted_from")
        if source is not None and not isinstance(source, Mapping):
            raise ValueError("converted issue media metadata must be an object")
        if isinstance(source, Mapping) and "sha256" in source:
            if not _valid_media_sha256(source["sha256"]):
                raise ValueError(
                    "converted issue media sha256 must be 64 lowercase hexadecimal characters"
                )


def _media_added(snapshot: dict, event: dict) -> None:
    """Append a media entry; a ``media_id`` already present is ignored (merged logs)."""
    data = event.get("data", {})
    media_id = data.get("media_id")
    media = snapshot.get("media", [])
    validate_issue_media_hashes({"media": [data]})
    if not media_id or any(m.get("id") == media_id for m in media):
        return
    entry: dict = {"id": media_id}
    entry.update({key: data[key] for key in MEDIA_ENTRY_FIELDS if key in data})
    entry["added_at"] = event.get("ts")
    entry["added_by"] = event.get("actor")
    snapshot["media"] = [*media, entry]


def _media_removed(snapshot: dict, event: dict) -> None:
    """Mark an entry removed and drop its original name from the snapshot; an
    unknown or already-removed ``media_id`` is ignored."""
    media_id = event.get("data", {}).get("media_id")
    for entry in snapshot.get("media", []):
        if entry.get("id") == media_id and not entry.get("removed"):
            entry.pop("original_name", None)
            entry["removed"] = {
                "at": event.get("ts"),
                "by": event.get("actor"),
                "reason": event.get("data", {}).get("reason"),
            }
            return


def redact_removed_media_names(events: Iterable[dict], snapshot: Mapping) -> list[dict]:
    """*events* as a command prints them: the ``original_name`` of media since
    removed is left out, as it is from the views. The log itself keeps it."""
    removed = {m.get("id") for m in snapshot.get("media", []) if m.get("removed")}
    shown = []
    for event in events:
        data = event.get("data") or {}
        if event.get("type") == "issue_media_added" and data.get("media_id") in removed:
            event = {**event, "data": {k: v for k, v in data.items() if k != "original_name"}}
        shown.append(event)
    return shown


_HANDLERS: dict[str, Callable[[dict, dict], None]] = {
    "issue_edited": _edited,
    "issue_comment_added": _comment_added,
    "issue_linked": _linked,
    "issue_unlinked": _unlinked,
    "issue_dismissed": _dismissed,
    "issue_marked_duplicate": _marked_duplicate,
    "issue_reopened": _reopened,
    "issue_media_added": _media_added,
    "issue_media_removed": _media_removed,
}


def apply_issue_event(snapshot: dict | None, event: dict) -> dict:
    """Return the snapshot after *event*; the input is not modified.

    An event type this version does not know only advances ``last_event_id``,
    so a log written by a newer Lattice still replays.
    """
    etype = event.get("type")
    if etype == "issue_filed":
        new = _filed(snapshot, event)
    else:
        if snapshot is None:
            raise ValueError(
                f"issue event {event.get('id')} ({etype}) comes before the issue was filed"
            )
        new = json.loads(json.dumps(snapshot))
        handler = _HANDLERS.get(etype)  # type: ignore[arg-type]
        if handler is None:
            new["last_event_id"] = event.get("id")
            return new
        handler(new, event)
    new["updated_at"] = event.get("ts")
    new["last_event_id"] = event.get("id")
    return new


def replay_issue(events: Iterable[dict]) -> dict | None:
    """The snapshot a whole log replays to (``None`` for an empty log)."""
    snapshot: dict | None = None
    for event in events:
        snapshot = apply_issue_event(snapshot, event)
    return snapshot


def serialize_issue_snapshot(snapshot: dict) -> str:
    """Sorted keys, 2-space indent, trailing newline (the snapshot convention)."""
    return json.dumps(snapshot, sort_keys=True, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Derived state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskInfo:
    """What the issue log needs to know about one linked task."""

    status: str | None
    erased: bool = False
    archived: bool = False
    short_id: str | None = None
    title: str | None = None


def is_closed(snapshot: dict) -> bool:
    return bool(snapshot.get("closure"))


def derive_issue_state(snapshot: dict, task_info: Mapping[str, TaskInfo | None]) -> str:
    """The issue's state, from its closure and its linked tasks' statuses.

    A closure wins. Otherwise only live links count: tasks that exist, are not
    erased, and are not cancelled. No live link is ``open``; every live link
    ``done`` is ``resolved``; anything else is ``linked``.
    """
    closure = snapshot.get("closure")
    if closure:
        return closure["kind"]
    statuses = []
    for link in snapshot.get("links", []):
        info = task_info.get(link["task_id"])
        if info is None or info.status is None or info.erased or info.status == "cancelled":
            continue
        statuses.append(info.status)
    if not statuses:
        return "open"
    if all(status == "done" for status in statuses):
        return "resolved"
    return "linked"


# ---------------------------------------------------------------------------
# Views and text
# ---------------------------------------------------------------------------


def issue_view(
    snapshot: dict,
    task_info: Mapping[str, TaskInfo | None],
    filed_origin: dict | None = None,
) -> dict:
    """The issue as commands print it under ``--json``: the snapshot plus its
    derived ``state`` and its linked ``tasks`` with their current statuses."""
    tasks = []
    for link in snapshot.get("links", []):
        info = task_info.get(link["task_id"])
        entry: dict = {
            "id": link["task_id"],
            "short_id": info.short_id if info else None,
            "title": info.title if info else None,
            "status": info.status if info else None,
            "linked_at": link.get("linked_at"),
            "linked_by": link.get("linked_by"),
        }
        if info and info.erased:
            entry["erased"] = True
        if info and info.archived:
            entry["archived"] = True
        tasks.append(entry)
    title, description = issue_title_description(snapshot)
    return {
        "id": snapshot["id"],
        "short_id": snapshot.get("short_id"),
        "seq": snapshot.get("seq"),
        "state": derive_issue_state(snapshot, task_info),
        "title": title,
        "description": description,
        "confidence": snapshot.get("confidence"),
        "evidence": list(snapshot.get("evidence", [])),
        "source": snapshot.get("source"),
        "filed_by": snapshot.get("filed_by"),
        "filed_at": snapshot.get("filed_at"),
        "filed_origin": filed_origin,
        "closure": snapshot.get("closure"),
        "tasks": tasks,
        "media": [dict(m) for m in snapshot.get("media", [])],
        "comment_count": snapshot.get("comment_count", 0),
        "updated_at": snapshot.get("updated_at"),
        "last_event_id": snapshot.get("last_event_id"),
    }


def first_line(text: str, limit: int = TITLE_LIMIT) -> str:
    """The first non-empty line of *text*, cut to *limit* characters."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(line) > limit:
        line = line[: limit - 3].rstrip() + "..."
    return line


def default_task_title(snapshot: dict) -> str:
    """The title ``promote`` gives a task when none is passed."""
    return issue_title_description(snapshot)[0]


#: Column widths: every state fits ``duplicate``, every confidence ``definite``.
STATE_WIDTH = max(len(state) for state in ISSUE_STATES)
CONFIDENCE_WIDTH = max(len(value) for value in CONFIDENCE_VALUES)


def id_width(items: Iterable[Mapping]) -> int:
    """The widest ``short_id`` among *items* (issues or task entries), for padding."""
    return max((len(str(i.get("short_id") or i.get("id") or "")) for i in items), default=0)


def task_status(entry: Mapping) -> str:
    """A view's task entry's status, ``erased`` or ``missing``."""
    if entry.get("erased"):
        return "erased"
    if entry.get("status") is None:
        return "missing"
    return entry["status"]


def task_label(entry: Mapping) -> str:
    """``LAT-370 (in_progress)`` for a view's task entry."""
    return f"{entry.get('short_id') or entry.get('id')} ({task_status(entry)})"


def format_issue_row(
    view: Mapping, id_width: int = 0, text_width: int = 60, activity: str | None = None
) -> str:
    """One ``issue list`` row: ID, state, confidence, title, and linked tasks.

    *id_width* pads the ID column (the caller passes the widest ID it prints);
    state and confidence pad to their widest possible value.
    """
    confidence = view.get("confidence") or "-"
    row = (
        f"{view.get('short_id') or view.get('id'):<{id_width}}  "
        f"{view['state']:<{STATE_WIDTH}}  {confidence:<{CONFIDENCE_WIDTH}}  "
    )
    row += first_line(view.get("title", ""), text_width)
    if view.get("tasks"):
        row += " -> " + ", ".join(task_label(t) for t in view["tasks"])
    if activity:
        row += f"  ({activity})"
    return row


def format_task_link_line(entry: Mapping, id_width: int = 0, status_width: int = 0) -> str:
    """One task line of ``issue show``: ID, status and title, padded like ``issue list``."""
    line = f"{entry.get('short_id') or entry.get('id'):<{id_width}}  "
    line += f"{task_status(entry):<{status_width}}"
    if entry.get("title"):
        line += f'  "{entry["title"]}"'
    return line


def linked_issue_summary(view: Mapping) -> dict:
    """An issue as ``lattice show <task> --json`` lists it under ``linked_issues``."""
    return {
        "id": view["id"],
        "short_id": view.get("short_id"),
        "state": view["state"],
        "title": view.get("title", ""),
    }


def format_linked_issue_line(item: Mapping, id_width: int = 0, text_width: int = 70) -> str:
    """One line of ``lattice show``'s ``Issues:`` section, padded like ``issue list``."""
    return (
        f"{item.get('short_id') or item.get('id'):<{id_width}}  "
        f"{item['state']:<{STATE_WIDTH}}  {first_line(item.get('title', ''), text_width)}"
    )


def promote_description(snapshots: Iterable[dict]) -> str:
    """The description of a task promoted from *snapshots*: one bullet per issue."""
    lines = ["Made from issue(s):", ""]
    for snap in snapshots:
        meta = [snap.get("confidence")] if snap.get("confidence") else []
        filer = get_actor_display(snap.get("filed_by") or "?")
        meta.append(f"filed by {filer} on {(snap.get('filed_at') or '?')[:10]}")
        title, description = issue_title_description(snap)
        description_lines = description.splitlines()
        lines.append(f"- {snap.get('short_id')} ({', '.join(meta)}): {title}")
        lines.extend(f"  {ln}" if ln.strip() else "" for ln in description_lines)
        if snap.get("evidence"):
            lines.append(f"  Evidence: {', '.join(snap['evidence'])}")
        summary = media_summary(present_media(snap))
        if summary:
            lines.append(f"  Media: {summary} (lattice issue media {snap.get('short_id')})")
        if snap.get("comment_count", 0):
            lines.append(
                f"  Comments: {snap['comment_count']} "
                f"(lattice issue show {snap.get('short_id') or snap.get('id')})"
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Title, description and comments
# ---------------------------------------------------------------------------


def split_title(raw: str, limit: int = TITLE_LIMIT) -> tuple[str, str, bool]:
    """Split issue text into its frozen title, overflow description and cut flag.

    This is the read contract for old ``text``-only issue logs as well as the
    filing rule for new titles. The cut flag is true only when the first line
    itself exceeds *limit*.

    For a long first line, take the greatest index ``i`` across the separators
    ``". "``, ``"; "`` and ``" — "`` with ``i >= 40`` and
    ``i + len(sep) <= limit``; the title is ``first[:i]`` (plus the full stop
    for ``". "``). Otherwise cut at the last space at or before *limit*, or
    at *limit* when there is no space.
    """
    normalized = raw.strip()
    lines = normalized.splitlines()
    first_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        return "", "", False
    first = lines[first_index].strip()
    rest = "\n".join(lines[first_index + 1 :]).strip()
    if len(first) <= limit:
        return first, rest, False

    candidate: tuple[int, str] | None = None
    for separator in (". ", "; ", " — "):
        index = first.rfind(separator, 0, limit)
        if index >= 40 and index + len(separator) <= limit:
            if candidate is None or index > candidate[0]:
                candidate = (index, separator)
    if candidate is not None:
        index, separator = candidate
        title = first[:index] + ("." if separator == ". " else "")
    else:
        space = first.rfind(" ", 0, limit + 1)
        title = first[:space] if space >= 0 else first[:limit]
    return title.rstrip(), normalized, True


def normalize_issue_description(description: str | None) -> str:
    """Strip trailing whitespace; represent whitespace-only descriptions as empty."""
    if description is None or not description.strip():
        return ""
    return description.rstrip()


def issue_title_description(snapshot: Mapping) -> tuple[str, str]:
    """The stored fields for new issues, or their frozen derivation for old logs."""
    if "title" in snapshot:
        return str(snapshot.get("title") or ""), str(snapshot.get("description") or "")
    title, description, _shortened = split_title(str(snapshot.get("text") or ""))
    return title, description


def check_edit_title(title: str) -> str:
    """Validate and trim a deliberate title edit; filing uses :func:`split_title`."""
    if "\n" in title or "\r" in title:
        raise ValueError("Issue title must be a single line.")
    value = title.strip()
    if not value:
        raise ValueError("Issue title must not be empty.")
    if len(value) > TITLE_LIMIT:
        raise ValueError(
            f"Title is {len(value)} characters; the limit is {TITLE_LIMIT}. "
            "Put the rest in --description."
        )
    return value


def _adapt_issue_comment_events(events: Iterable[dict]) -> list[dict]:
    return [
        {**event, "type": event["type"].removeprefix("issue_")}
        for event in events
        if str(event.get("type", "")).startswith("issue_comment_")
    ]


def issue_comments(events: Iterable[dict]) -> list[dict]:
    """Materialize an issue's ``issue_comment_*`` events in the task comment shape."""
    adapted = _adapt_issue_comment_events(events)
    comments = materialize_comments(adapted)
    origins = {event.get("id"): issue_origin(event.get("origin")) for event in adapted}

    def add_origin(comment: dict) -> None:
        comment["origin"] = origins.get(comment.get("id"))
        for reply in comment.get("replies", []):
            add_origin(reply)

    for comment in comments:
        add_origin(comment)
    return comments


def issue_comment_events(events: Iterable[dict]) -> list[dict]:
    """The task-named event copies used by the shared comment validators."""
    return _adapt_issue_comment_events(events)


def issue_origin(origin: object) -> dict | None:
    """The user and machine shown for a filing or comment, or ``None`` if absent."""
    if not isinstance(origin, dict):
        return None
    from lattice.core.origin import _user_and_machine

    user, machine = _user_and_machine(origin)
    return {"user": user, "machine": machine}


def actor_with_origin(actor: str | dict | None, origin: object) -> str:
    """A filing/comment author plus the reported origin pair when one exists."""
    name = get_actor_display(actor or "?")
    if isinstance(origin, dict) and ("user" in origin or "machine" in origin):
        fields = {"user": origin.get("user"), "machine": origin.get("machine")}
    else:
        fields = issue_origin(origin)
    if fields is None:
        return name
    user, machine = fields["user"], fields["machine"]
    if user and machine:
        return f"{name} · {user}@{machine}"
    if user or machine:
        return f"{name} · {user or machine}"
    return name


def actor_matches(actor: object, requested: str) -> bool:
    """Match a bare actor name/session or its canonical and legacy actor key."""
    if isinstance(actor, dict):
        name = actor.get("name")
        base_name = actor.get("base_name")
        prefix = "human" if actor.get("model") == "human" else "agent"
        aliases = {name, base_name, actor.get("session")}
        aliases.update(f"{prefix}:{value}" for value in (name, base_name) if value)
        return requested in aliases
    if isinstance(actor, str):
        prefix, separator, name = actor.partition(":")
        return actor == requested or bool(
            separator and prefix in {"human", "agent"} and name == requested
        )
    return False


def issue_activity(events: Iterable[dict], actor: str) -> str | None:
    """Return ``filed`` or ``commented`` when *actor* appears in an issue log."""
    filed = False
    commented = False
    for event in events:
        etype = event.get("type")
        matches = actor_matches(event.get("actor"), actor)
        filed |= etype == "issue_filed" and matches
        commented |= etype == "issue_comment_added" and matches
    if filed:
        return "filed"
    if commented:
        return "commented"
    return None
