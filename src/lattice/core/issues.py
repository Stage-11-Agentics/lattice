"""The optional issue log (LAT-361): pure logic, no filesystem.

An issue is an observation someone filed ("the footer overlaps the CTA at
400px"), kept apart from tasks, which are commitments. Each issue has its own
event log; its snapshot is a replay of that log. Links to tasks are recorded on
the issue only. The state (open, linked, resolved, dismissed, duplicate) is
never stored: :func:`derive_issue_state` computes it from the snapshot and the
linked tasks' current statuses.

The display ID format lives in :func:`format_issue_short_id` and
:func:`parse_issue_ref` and nowhere else.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

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
    """The ``ISSUES_DISABLED`` message; *has_existing*: the board has an ``issues/`` directory."""
    message = (
        "The issue log is off for this project. The board owner turns it on by adding "
        f"{ENABLE_LINE} to .lattice/config.json."
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
        "text": data.get("text", ""),
        "filed_by": event.get("actor"),
        "filed_at": event.get("ts"),
        "links": [],
        "closure": None,
    }
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
    """Reject malformed content hashes in persisted media entries."""
    entries = snapshot.get("media", [])
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if "sha256" in entry and not _valid_media_sha256(entry["sha256"]):
            raise ValueError("issue media sha256 must be 64 lowercase hexadecimal characters")
        source = entry.get("converted_from")
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


def issue_view(snapshot: dict, task_info: Mapping[str, TaskInfo | None]) -> dict:
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
    return {
        "id": snapshot["id"],
        "short_id": snapshot.get("short_id"),
        "seq": snapshot.get("seq"),
        "state": derive_issue_state(snapshot, task_info),
        "text": snapshot.get("text", ""),
        "confidence": snapshot.get("confidence"),
        "evidence": list(snapshot.get("evidence", [])),
        "source": snapshot.get("source"),
        "filed_by": snapshot.get("filed_by"),
        "filed_at": snapshot.get("filed_at"),
        "closure": snapshot.get("closure"),
        "tasks": tasks,
        "media": [dict(m) for m in snapshot.get("media", [])],
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
    return first_line(snapshot.get("text", ""))


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


def format_issue_row(view: Mapping, id_width: int = 0, text_width: int = 60) -> str:
    """One ``issue list`` row: ID, state, confidence, text, and linked tasks.

    *id_width* pads the ID column (the caller passes the widest ID it prints);
    state and confidence pad to their widest possible value.
    """
    confidence = view.get("confidence") or "-"
    row = (
        f"{view.get('short_id') or view.get('id'):<{id_width}}  "
        f"{view['state']:<{STATE_WIDTH}}  {confidence:<{CONFIDENCE_WIDTH}}  "
    )
    row += first_line(view.get("text", ""), text_width)
    if view.get("tasks"):
        row += " -> " + ", ".join(task_label(t) for t in view["tasks"])
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
        "text": view.get("text", ""),
    }


def format_linked_issue_line(item: Mapping, id_width: int = 0, text_width: int = 70) -> str:
    """One line of ``lattice show``'s ``Issues:`` section, padded like ``issue list``."""
    return (
        f"{item.get('short_id') or item.get('id'):<{id_width}}  "
        f"{item['state']:<{STATE_WIDTH}}  {first_line(item.get('text', ''), text_width)}"
    )


def promote_description(snapshots: Iterable[dict]) -> str:
    """The description of a task promoted from *snapshots*: one bullet per issue."""
    lines = ["Made from issue(s):", ""]
    for snap in snapshots:
        meta = [snap.get("confidence")] if snap.get("confidence") else []
        filer = get_actor_display(snap.get("filed_by") or "?")
        meta.append(f"filed by {filer} on {(snap.get('filed_at') or '?')[:10]}")
        text_lines = (snap.get("text") or "").strip().splitlines() or [""]
        lines.append(f"- {snap.get('short_id')} ({', '.join(meta)}): {text_lines[0]}")
        lines.extend(f"  {ln}" if ln.strip() else "" for ln in text_lines[1:])
        if snap.get("evidence"):
            lines.append(f"  Evidence: {', '.join(snap['evidence'])}")
        summary = media_summary(present_media(snap))
        if summary:
            lines.append(f"  Media: {summary} (lattice issue media {snap.get('short_id')})")
    return "\n".join(lines) + "\n"
