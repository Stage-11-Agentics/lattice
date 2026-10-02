"""The optional issue log (LAT-361): ``lattice issue file|list|show|promote|link|...``.

Issues are observations, kept apart from tasks, which are commitments. The log
is off unless ``.lattice/config.json`` has ``"issues": {"enabled": true}``, and
works only on local boards. Writes run the ``issue.*`` operations; ``list``
and ``show`` read the files directly.
"""

from __future__ import annotations

import click

from lattice.cli.helpers import (
    common_options,
    json_envelope,
    load_project_config,
    output_error,
    output_result,
    resolve_body,
    require_root,
)
from lattice.cli.main import cli
from lattice.cli.ops_bridge import provenance_params, run_operation
from lattice.core.errors import OpError


def _require_issue_log(is_json: bool) -> tuple:
    """``(lattice_dir, config)`` when the issue log can be used here, else the
    command's error: ``LOCAL_ONLY`` on a bound checkout (before anything is
    read or fetched), ``NOT_INITIALIZED``, or ``ISSUES_DISABLED``."""
    from lattice.boards import hosted_binding
    from lattice.core.config import issues_enabled
    from lattice.core.issues import issues_disabled_message
    from lattice.storage.issues import issues_dir

    try:
        binding = hosted_binding(None)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    if binding is not None:
        output_error(
            "The issue log works only on local boards for now; this checkout's board "
            f"lives on the server ('{binding}').",
            "LOCAL_ONLY",
            is_json,
        )
    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    if not issues_enabled(config):
        output_error(
            issues_disabled_message(issues_dir(lattice_dir).is_dir()), "ISSUES_DISABLED", is_json
        )
    return lattice_dir, config


def _write(op_name: str, params: dict, is_json: bool, checked: tuple | None = None) -> tuple:
    """``(lattice_dir, result)`` of running *op_name* after the issue-log checks."""
    lattice_dir, config = checked or _require_issue_log(is_json)
    return lattice_dir, run_operation(op_name, params, is_json, config=config)


def _name(view: dict) -> str:
    return view.get("short_id") or view["id"]


def _task_entry(view: dict, raw_task: str) -> dict | None:
    """The view's entry for the task the caller named (a ULID or a short ID)."""
    wanted = raw_task.upper()
    return next(
        (
            t
            for t in view.get("tasks", [])
            if t["id"].upper() == wanted or (t.get("short_id") or "").upper() == wanted
        ),
        None,
    )


def _warn_unreadable(path, exc: OpError) -> None:  # noqa: ANN001
    """Skip an unreadable issue file with one line on stderr (stdout stays clean)."""
    from lattice.core.issues import unreadable_issue_warning

    click.echo(unreadable_issue_warning(path, exc), err=True)


def _read_stdin_text() -> str:
    import sys

    return sys.stdin.read().rstrip()


# ---------------------------------------------------------------------------
# Media (LAT-366): photos and videos copied into the issue
# ---------------------------------------------------------------------------

_KEPT_TEXT = {
    "not_media": "not a photo or video by its content",
    "directory": "a directory",
    "not_found": "no such file",
}


def _heic_unconverted_text() -> str:
    from lattice.core.issue_media import HEIC_HINT

    return f"a HEIC photo, and neither sips nor ffmpeg could convert it; convert it: {HEIC_HINT}"


def _classify(arg: str) -> tuple[str, object, str | None]:
    """``(what, path, content_type)`` for one ``--evidence`` or ``attach`` argument.

    *what* is ``url``, ``not_found``, ``directory``, ``not_media``, ``heic`` or
    ``media``. Symlinks are followed: it is the filer's own file.
    """
    import os
    import stat
    from pathlib import Path

    from lattice.core.issue_media import SNIFF_BYTES, sniff_heic, sniff_media

    if arg.startswith(("http://", "https://")):
        return "url", None, None
    path = Path(os.path.expanduser(arg))
    try:
        mode = os.stat(path).st_mode
    except (OSError, ValueError):
        return "not_found", path, None
    if stat.S_ISDIR(mode):
        return "directory", path, None
    if not stat.S_ISREG(mode):
        return "not_found", path, None
    try:
        with open(path, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        return "not_found", path, None
    content_type = sniff_media(head)
    if content_type is not None:
        return "media", path, content_type
    if sniff_heic(head):
        return "heic", path, "image/heic"
    return "not_media", path, None


def _refuse_too_large(name: str, size: int, limit: int, video: bool, nothing: str, is_json: bool):  # noqa: ANN202
    from lattice.core.issue_media import file_too_large_message

    message = file_too_large_message(name, size, limit, nothing=nothing)
    if video:
        stem = name.rsplit(".", 1)[0] or "video"
        message += (
            " Shorten or compress it, for example: "
            f"ffmpeg -i {name} -vf scale=1280:-2 -crf 30 {stem}-small.mp4"
        )
    output_error(message, "PAYLOAD_TOO_LARGE", is_json)


def _read_media_file(path, limit: int, nothing: str, is_json: bool, video: bool) -> bytes:  # noqa: ANN001
    """The file's bytes, refused unread when ``stat`` says it is over *limit*; at
    most *limit* + 1 bytes are read, which also catches a file that grows."""
    import os

    size = os.stat(path).st_size
    if size > limit:
        _refuse_too_large(path.name, size, limit, video, nothing, is_json)
    try:
        with open(path, "rb") as fh:
            content = fh.read(limit + 1)
    except OSError as exc:
        output_error(f"Cannot read {path}: {exc}.", "VALIDATION_ERROR", is_json)
    if len(content) > limit:
        _refuse_too_large(path.name, len(content), limit, video, nothing, is_json)
    return content


def _prepare_media(
    arg: str,
    path,
    content_type: str,
    limit: int,
    nothing: str,
    is_json: bool,  # noqa: ANN001
) -> dict | None:
    """One file, ready to send: ``{"item", "name", "arg", "hashes", "notes", "sizes"}``.

    A video is transcoded and gets frames when ffmpeg is present; a HEIC photo is
    converted to JPEG. ``None`` for a HEIC photo nothing could convert.
    """
    import hashlib

    from lattice.core.issue_media import clean_original_name, frame_name, media_kind
    from lattice.ops.task_attach import encode_payload

    name = clean_original_name(path.name) or "file"
    video = media_kind(content_type) == "video"
    content = _read_media_file(path, limit, nothing, is_json, video)
    sha256 = hashlib.sha256(content).hexdigest()
    record: dict = {"arg": arg, "name": name, "hashes": {sha256}, "notes": [], "sizes": None}
    item: dict
    if content_type == "image/heic":
        from lattice.integrations.ffmpeg import convert_heic

        converted = convert_heic(path)
        if converted is None:
            return None
        item = {
            "payload": encode_payload(name, converted),
            "converted_from": {
                "content_type": "image/heic",
                "size_bytes": len(content),
                "sha256": sha256,
            },
        }
        record["notes"].append(("converted", "heic"))
    elif video:
        from lattice.integrations.ffmpeg import ffmpeg_state, prepare_video

        prepared = prepare_video(path, content, content_type, sha256)
        if len(prepared.content) > limit:
            _refuse_too_large(name, len(prepared.content), limit, True, nothing, is_json)
        item = {"payload": encode_payload(name, prepared.content)}
        if prepared.video:
            item["video"] = prepared.video
        if prepared.frames:
            item["frames"] = [
                {"t_ms": t_ms, "payload": encode_payload(frame_name(t_ms), data)}
                for t_ms, data in prepared.frames
            ]
        if prepared.converted_from:
            item["converted_from"] = prepared.converted_from
            record["sizes"] = (len(content), len(prepared.content))
        notes = list(prepared.notes)
        if ("no_frames", "ffmpeg_not_found") in notes and ffmpeg_state() == "off":
            notes[notes.index(("no_frames", "ffmpeg_not_found"))] = ("no_frames", "ffmpeg_off")
        record["notes"] = notes
    else:
        item = {"payload": encode_payload(name, content)}
    record["item"] = item
    return record


def _collect_evidence(
    evidence: tuple[str, ...], config: dict, is_json: bool
) -> tuple[list[str], list[dict], list[dict]]:
    """``(pointers, media records, kept-as-text notes)`` for ``issue file --evidence``.

    URLs and anything that is not a photo or video stay text pointers, as before;
    photos and videos are prepared to be copied in. The same content twice is
    sent once. A file over the per-file limit refuses the whole command.
    """
    from lattice.core.issue_media import media_limits

    limit, _per_issue = media_limits(config)
    pointers: list[str] = []
    records: list[dict] = []
    kept: list[dict] = []
    seen: set[str] = set()
    for arg in evidence:
        what, path, content_type = _classify(arg)
        if what in ("media", "heic"):
            record = _prepare_media(arg, path, content_type, limit, "Nothing was filed.", is_json)
            if record is None:
                pointers.append(arg)
                kept.append({"evidence": arg, "kept_as": "text", "reason": "heic_unconverted"})
                continue
            if record["hashes"] & seen:
                continue
            seen |= record["hashes"]
            records.append(record)
            continue
        pointers.append(arg)
        if what != "url":
            kept.append({"evidence": arg, "kept_as": "text", "reason": what})
    return pointers, records, kept


def _check_attach_args(files: tuple[str, ...], is_json: bool) -> list[tuple]:
    """Every ``attach`` argument must be an existing photo or video (all or nothing)."""
    from lattice.core.issue_media import ACCEPTED_FORMATS_TEXT

    checked = []
    for arg in files:
        what, path, content_type = _classify(arg)
        if what == "not_found" or what == "url":
            output_error(f"No such file: {arg}.", "VALIDATION_ERROR", is_json)
        if what == "directory":
            output_error(
                f"{arg} is a directory, not a photo or video. Accepted: {ACCEPTED_FORMATS_TEXT}.",
                "VALIDATION_ERROR",
                is_json,
            )
        if what == "not_media":
            output_error(
                f"{arg} is not a photo or video by its content. Accepted: "
                f"{ACCEPTED_FORMATS_TEXT}.",
                "VALIDATION_ERROR",
                is_json,
            )
        checked.append((arg, path, content_type))
    return checked


def _media_entry_for(view: dict, record: dict) -> dict | None:
    """The view's present media entry holding *record*'s content."""
    for entry in view.get("media", []):
        if entry.get("removed"):
            continue
        hashes = {entry.get("sha256"), (entry.get("converted_from") or {}).get("sha256")}
        if hashes & record["hashes"]:
            return entry
    return None


def _media_notes(
    view: dict, records: list[dict], kept: list[dict], added_ids: set[str]
) -> tuple[list[dict], list[str]]:
    """``(data.notes, the human note lines)`` after a write that sent *records*."""
    from lattice.core.issue_media import format_size

    notes: list[dict] = []
    lines: list[str] = []
    for record in records:
        entry = _media_entry_for(view, record) or {}
        n, name = entry.get("n"), record["name"]
        if entry and entry.get("id") not in added_ids:
            notes.append({"evidence": record["arg"], "media_n": n, "reason": "duplicate"})
            lines.append(f"{_name(view)} already has {name} (media {n})")
            continue
        for reason, detail in record["notes"]:
            note: dict = {"evidence": record["arg"], "media_n": n, "reason": reason}
            if detail:
                note["detail"] = detail
            if reason == "transcoded" and record["sizes"]:
                before, after = record["sizes"]
                note.update(from_size_bytes=before, size_bytes=after)
                lines.append(
                    f"{name}: {format_size(before)} recording, stored as "
                    f"{format_size(after)} (H.264)"
                )
            elif reason == "converted":
                lines.append(f"{name}: converted from HEIC to JPEG")
            elif reason == "remuxed" and detail == "metadata_stripped":
                lines.append(
                    f"{name}: remuxed without metadata (already H.264, smaller than re-encoded)"
                )
            elif reason == "not_transcoded":
                lines.append(f"{name}: stored as it is; ffmpeg could not transcode it")
            elif reason == "one_frame":
                lines.append(f"{name}: its length is unknown, so it has one frame, at 0:00")
            elif reason == "no_frames":
                lines.append(f"no frames for {name}: {_NO_FRAMES_DETAIL[detail]}")
            notes.append(note)
    for note in kept:
        notes.append(note)
        why = _KEPT_TEXT.get(note["reason"]) or _heic_unconverted_text()
        lines.append(f"kept as text: {note['evidence']} ({why})")
    return notes, lines


_NO_FRAMES_DETAIL = {
    "ffmpeg_not_found": "ffmpeg not found (install ffmpeg, or set LATTICE_FFMPEG)",
    "ffmpeg_off": "ffmpeg is off (LATTICE_FFMPEG=off)",
    "ffmpeg_failed": "ffmpeg could not read it",
}


def _print_write(
    view: dict, notes: list[dict], lines: list[str], message: str, is_json: bool, quiet: bool
) -> None:
    """A media write's output: notes under ``data.notes``, on stdout after the
    message, or on stderr under ``--quiet``."""
    if is_json:
        click.echo(json_envelope(True, data={**view, "notes": notes}))
        return
    if quiet:
        click.echo(_name(view))
        for line in lines:
            click.echo(line, err=True)
        return
    click.echo(message)
    for line in lines:
        click.echo(f"  {line}")


# ---------------------------------------------------------------------------
# The group
# ---------------------------------------------------------------------------


@cli.group()
def issue() -> None:
    """The issue log: file observations, discuss them, then promote or link them to tasks.

    Optional and off by default. The board owner turns it on by adding
    "issues": {"enabled": true} to .lattice/config.json. Local boards only.
    """


@issue.command("file")
@click.argument("title")
@click.option("--description", default=None, help="A longer explanation of the issue.")
@click.option("--description-file", default=None, help="Read the description from a file.")
@click.option(
    "--confidence",
    type=click.Choice(["possible", "definite"]),
    default=None,
    help="How sure the filer is.",
)
@click.option(
    "--evidence",
    multiple=True,
    help="A path or URL backing it up (repeatable). Photos and videos are copied in.",
)
@click.option("--source", default=None, help="Where it came from (e.g., tester-round-8).")
@common_options
def issue_file(
    title: str,
    description: str | None,
    description_file: str | None,
    confidence: str | None,
    evidence: tuple[str, ...],
    source: str | None,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """File an issue with a short title and optional description.

    TITLE '-' reads stdin. A long or multi-line title is split into a title and
    description; an explicit description is appended after the overflow.

    A photo or video passed as --evidence (decided by its content: PNG, JPEG,
    GIF, WebP; MP4, MOV, WebM; HEIC is converted to JPEG) is copied into the
    issue. With ffmpeg, a video is re-encoded to H.264 and gets still frames an
    agent can read ('lattice issue media <issue> --paths').
    """
    is_json = output_json
    checked = _require_issue_log(is_json)
    if description is not None and description_file is not None:
        output_error(
            "Provide either --description or --description-file, not both.",
            "VALIDATION_ERROR",
            is_json,
        )
    if title == "-" and description == "-":
        output_error(
            "Only one of TITLE and --description can read stdin.", "VALIDATION_ERROR", is_json
        )
    if title == "-":
        title = _read_stdin_text()
    if description is not None or description_file is not None:
        description = resolve_body(
            description,
            description_file,
            is_json,
            what="issue description",
            arg_label="--description",
        )
        if description == "-":
            description = _read_stdin_text()
    pointers, records, kept = _collect_evidence(evidence, checked[1], is_json)
    _lattice_dir, result = _write(
        "issue.file",
        {
            "title": title,
            "description": description,
            "confidence": confidence,
            "evidence": tuple(pointers),
            "source": source,
            "media": tuple(r["item"] for r in records),
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
        checked,
    )
    from lattice.core.issue_media import media_summary
    from lattice.core.issues import TITLE_LIMIT, first_line, split_title

    view = result.value
    added = {e["data"]["media_id"] for e in result.events if e["type"] == "issue_media_added"}
    notes, lines = _media_notes(view, records, kept, added)
    if split_title(title)[2]:
        notes.append({"reason": "title_shortened", "limit": TITLE_LIMIT})
        lines.append(
            f"title shortened to {TITLE_LIMIT} characters; the full text is in the description"
        )
    message = f"Filed {_name(view)}: {first_line(view['title'])}"
    summary = media_summary(view.get("media", []))
    if summary:
        message += f" ({summary})"
    _print_write(view, notes, lines, message, is_json, quiet)


@issue.command("list")
@click.option(
    "--state",
    "states",
    multiple=True,
    type=click.Choice(["open", "linked", "resolved", "dismissed", "duplicate"]),
    help="Show only issues in this state (repeatable). Default: open and linked.",
)
@click.option("--all", "show_all", is_flag=True, help="Show every issue, closed ones too.")
@click.option(
    "--by", "by_actor", default=None, help="Show every issue filed or commented on by this actor."
)
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def issue_list(
    states: tuple[str, ...], show_all: bool, by_actor: str | None, output_json: bool
) -> None:
    """List issues: open, then linked, each oldest first."""
    from lattice.core.issues import (
        DEFAULT_LIST_STATES,
        ISSUE_STATES,
        actor_matches,
        format_issue_row,
        id_width,
        issue_activity,
    )
    from lattice.storage.issues import issue_views, list_issue_snapshots, read_issue_events

    is_json = output_json
    lattice_dir, _config = _require_issue_log(is_json)
    snapshots = list_issue_snapshots(lattice_dir, on_unreadable=_warn_unreadable)
    views = issue_views(lattice_dir, snapshots)
    wanted = ISSUE_STATES if show_all or by_actor is not None else (states or DEFAULT_LIST_STATES)
    order = {state: i for i, state in enumerate(ISSUE_STATES)}
    shown = []
    for view in views:
        if view["state"] not in wanted:
            continue
        if by_actor is not None:
            activity = "filed" if actor_matches(view.get("filed_by"), by_actor) else None
            if activity is None:
                try:
                    activity = issue_activity(read_issue_events(lattice_dir, view["id"]), by_actor)
                except OpError as exc:
                    from lattice.storage.issues import issues_dir

                    _warn_unreadable(
                        issues_dir(lattice_dir) / "events" / f"{view['id']}.jsonl", exc
                    )
                    continue
            if activity is None:
                continue
            view = {**view, "activity": activity}
        shown.append(view)
    shown.sort(key=lambda v: (order[v["state"]], v.get("seq") or 0))
    if is_json:
        click.echo(json_envelope(True, data=shown))
        return
    width = id_width(shown)
    for view in shown:
        click.echo(format_issue_row(view, width, activity=view.get("activity")))
    counts = {state: sum(1 for v in shown if v["state"] == state) for state in ISSUE_STATES}
    summary = ", ".join(f"{counts[s]} {s}" for s in ISSUE_STATES if s in wanted)
    footer = f"{len(shown)} issue{'s' if len(shown) != 1 else ''} ({summary})"
    hidden = len(views) - len(shown)
    if hidden and not show_all:
        footer += f"; {hidden} other{'s' if hidden != 1 else ''} hidden (--all to show)"
    click.echo(footer)


@issue.command("show")
@click.argument("issue_id")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def issue_show(issue_id: str, output_json: bool) -> None:
    """Show one issue: its title, description, evidence, media, state, linked tasks, comments and history."""
    from lattice.core.events import get_actor_display
    from lattice.core.issues import (
        actor_with_origin,
        format_comment_lines,
        format_task_link_line,
        id_width,
        task_status,
    )
    from lattice.storage.issues import (
        issue_detail,
        read_issue_snapshot,
    )

    is_json = output_json
    lattice_dir, _config = _require_issue_log(is_json)
    try:
        view = issue_detail(lattice_dir, issue_id, on_unreadable=_warn_unreadable)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    if view is None:
        output_error(f"Issue '{issue_id}' not found.", "NOT_FOUND", is_json)
    events = view["events"]
    if is_json:
        click.echo(json_envelope(True, data=view))
        return

    click.echo(f'{_name(view)} ({view["id"]})  "{view["title"]}"')
    click.echo(f"State: {view['state']}")
    click.echo(
        f"Filed: {view['filed_at']} by "
        f"{actor_with_origin(view['filed_by'], view.get('filed_origin'))}"
    )
    if view.get("confidence"):
        click.echo(f"Confidence: {view['confidence']}")
    if view.get("source"):
        click.echo(f"Source: {view['source']}")
    if view["description"]:
        click.echo("")
        click.echo("Description:")
        for line in view["description"].splitlines():
            click.echo(f"  {line}")
    if view["evidence"]:
        click.echo("")
        click.echo("Evidence:")
        for item in view["evidence"]:
            click.echo(f"  {item}")
    if view.get("media"):
        from lattice.core.issue_media import format_media_lines

        click.echo("")
        click.echo("Media:")
        for line in format_media_lines(view["media"], get_actor_display):
            click.echo(f"  {line}")
    if view["tasks"]:
        click.echo("")
        click.echo("Tasks:")
        widths = (
            id_width(view["tasks"]),
            max(len(task_status(t)) for t in view["tasks"]),
        )
        for task in view["tasks"]:
            by = get_actor_display(task.get("linked_by") or "?")
            line = format_task_link_line(task, *widths)
            click.echo(f"  {line}  (linked {task.get('linked_at')} by {by})")
    closure = view.get("closure")
    if closure:
        click.echo("")
        by = get_actor_display(closure.get("by") or "?")
        if closure["kind"] == "duplicate":
            original = read_issue_snapshot(lattice_dir, closure["duplicate_of"]) or {}
            what = f"duplicate of {original.get('short_id') or closure['duplicate_of']}"
        else:
            what = f"dismissed: {closure.get('reason')}"
        click.echo(f"Closed: {what} ({closure.get('at')} by {by})")
    if view.get("comment_count", 0):
        click.echo("")
        click.echo(f"Comments ({view['comment_count']}):")
        for line in format_comment_lines(view["comments"]):
            click.echo(line)
    click.echo("")
    click.echo("History:")
    for event in events:
        actor = actor_with_origin(event.get("actor"), event.get("origin"))
        click.echo(
            f"  {event.get('ts')}  {event.get('type')}  "
            f"{actor or get_actor_display(event.get('actor') or '?')}"
        )


@issue.command("promote")
@click.argument("issue_ids", nargs=-1, required=True)
@click.option("--title", default=None, help="The task's title (default: the first issue's title).")
@click.option("--priority", default=None, help="The task's priority.")
@click.option("--type", "task_type", default=None, help="The task's type.")
@common_options
def issue_promote(
    issue_ids: tuple[str, ...],
    title: str | None,
    priority: str | None,
    task_type: str | None,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Create one backlog task from one or more issues and link them to it."""
    is_json = output_json
    _lattice_dir, result = _write(
        "issue.promote",
        {
            "issues": issue_ids,
            "title": title,
            "priority": priority,
            "type": task_type,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    task = result.value["task"]
    task_name = task.get("short_id") or task["id"]
    names = ", ".join(_name(v) for v in result.value["issues"])
    output_result(
        data=result.value,
        human_message=f'Created {task_name} "{task["title"]}" from {names}',
        quiet_value=task_name,
        is_json=is_json,
        is_quiet=quiet,
    )


def _issue_task_command(op_name: str, verb: str):  # noqa: ANN202
    """``issue link`` and ``issue unlink``: ISSUE_ID TASK_ID and the write options."""

    @click.argument("issue_id")
    @click.argument("task_id")
    @common_options
    def command(
        issue_id: str,
        task_id: str,
        output_json: bool,
        quiet: bool,
        session: str | None,
        model: str | None,
        triggered_by: str | None,
        on_behalf_of: str | None,
        provenance_reason: str | None,
    ) -> None:
        from lattice.core.issues import task_label

        is_json = output_json
        _lattice_dir, result = _write(
            op_name,
            {
                "issue": issue_id,
                "task": task_id,
                **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
            },
            is_json,
        )
        view = result.value
        if op_name == "issue.link":
            entry = _task_entry(view, task_id)
            label = task_label(entry) if entry else task_id
            message = (
                f"{_name(view)} is already linked to {label}"
                if result.idempotent
                else f"Linked {_name(view)} to {label}"
            )
        else:
            message = (
                f"{_name(view)} is not linked to {task_id}"
                if result.idempotent
                else f"Unlinked {_name(view)} from {task_id}"
            )
        output_result(
            data=view,
            human_message=message,
            quiet_value=_name(view),
            is_json=is_json,
            is_quiet=quiet,
        )

    command.__doc__ = verb
    return command


issue.command("link")(
    _issue_task_command("issue.link", "Link an issue to a task (active or archived).")
)
issue.command("unlink")(_issue_task_command("issue.unlink", "Remove an issue's link to a task."))


def _closing_command(op_name: str, doc: str, *, of: bool = False):  # noqa: ANN202
    """``issue dismiss``, ``issue duplicate`` and ``issue reopen``."""

    def body(
        issue_id: str,
        of_id: str | None,
        output_json: bool,
        quiet: bool,
        session: str | None,
        model: str | None,
        triggered_by: str | None,
        on_behalf_of: str | None,
        provenance_reason: str | None,
    ) -> None:
        is_json = output_json
        params = {
            "issue": issue_id,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        }
        if of:
            params["of"] = of_id
        lattice_dir, result = _write(op_name, params, is_json)
        view = result.value
        closure = view.get("closure") or {}
        if op_name == "issue.dismiss":
            message = f"Dismissed {_name(view)}: {closure.get('reason')}"
        elif op_name == "issue.duplicate":
            from lattice.storage.issues import read_issue_snapshot

            original = read_issue_snapshot(lattice_dir, closure["duplicate_of"])
            original_name = (original or {}).get("short_id") or closure["duplicate_of"]
            message = f"Marked {_name(view)} as a duplicate of {original_name}"
        else:
            message = f"Reopened {_name(view)}"
        output_result(
            data=view,
            human_message=message,
            quiet_value=_name(view),
            is_json=is_json,
            is_quiet=quiet,
        )

    if of:

        @click.argument("issue_id")
        @click.option("--of", "of_id", required=True, help="The issue this one repeats.")
        @common_options
        def command(issue_id: str, of_id: str, **kwargs) -> None:  # noqa: ANN003
            body(issue_id, of_id, **kwargs)

    else:

        @click.argument("issue_id")
        @common_options
        def command(issue_id: str, **kwargs) -> None:  # noqa: ANN003
            body(issue_id, None, **kwargs)

    command.__doc__ = doc
    return command


issue.command("dismiss")(
    _closing_command("issue.dismiss", "Close an issue as not worth acting on. Requires --reason.")
)
issue.command("duplicate")(
    _closing_command("issue.duplicate", "Close an issue as a duplicate of another.", of=True)
)
issue.command("reopen")(_closing_command("issue.reopen", "Reopen a dismissed or duplicate issue."))


@issue.command("attach")
@click.argument("issue_id")
@click.argument("files", nargs=-1, required=True)
@common_options
def issue_attach(
    issue_id: str,
    files: tuple[str, ...],
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Add photos and videos to an issue after filing (closed issues too).

    All or nothing: every FILE must be a photo or video by its content (PNG,
    JPEG, GIF, WebP; MP4, MOV, WebM; HEIC is converted to JPEG). Content the
    issue already holds is skipped.
    """
    from lattice.core.issue_media import HEIC_HINT, media_limits, media_summary

    is_json = output_json
    checked = _require_issue_log(is_json)
    limit, _per_issue = media_limits(checked[1])
    records: list[dict] = []
    seen: set[str] = set()
    for arg, path, content_type in _check_attach_args(files, is_json):
        record = _prepare_media(arg, path, content_type, limit, "Nothing was attached.", is_json)
        if record is None:
            output_error(
                f"{arg} is a HEIC photo, and neither sips nor ffmpeg could convert it to JPEG. "
                f"Convert it, then attach the JPEG: {HEIC_HINT}",
                "VALIDATION_ERROR",
                is_json,
            )
        if record["hashes"] & seen:
            continue
        seen |= record["hashes"]
        records.append(record)
    _lattice_dir, result = _write(
        "issue.attach",
        {
            "issue": issue_id,
            "media": tuple(r["item"] for r in records),
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
        checked,
    )
    view = result.value
    added_ids = {e["data"]["media_id"] for e in result.events if e["type"] == "issue_media_added"}
    notes, lines = _media_notes(view, records, [], added_ids)
    added = [m for m in view.get("media", []) if m.get("id") in added_ids]
    message = (
        f"Attached to {_name(view)}: {media_summary(added)}"
        if added
        else f"Nothing attached to {_name(view)}"
    )
    _print_write(view, notes, lines, message, is_json, quiet)


@issue.command("detach")
@click.argument("issue_id")
@click.argument("media")
@common_options
def issue_detach(
    issue_id: str,
    media: str,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Remove one photo or video from an issue for good. Requires --reason.

    MEDIA is its number on the issue (see 'lattice issue media') or its med_ ID.
    The file and its frames are deleted; the log keeps who, when and why. If
    .lattice/ is tracked in git, the file stays in git history.
    """
    is_json = output_json
    checked = _require_issue_log(is_json)
    before = _media_before_detach(checked[0], issue_id, media)
    _lattice_dir, result = _write(
        "issue.detach",
        {
            "issue": issue_id,
            "media": media,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
        checked,
    )
    view = result.value
    if result.idempotent:
        message = f"Media {media} of {_name(view)} was already removed"
    else:
        entry, frames = before or ({}, 0)
        what = ", ".join(x for x in (entry.get("kind"), entry.get("original_name")) if x)
        n = entry.get("n", media)
        deleted = (
            f"its file and {frames} frame{'s' if frames != 1 else ''} are"
            if frames
            else ("its file is")
        )
        message = (
            f"Removed media {n}{f' ({what})' if what else ''} from {_name(view)}; "
            f"{deleted} deleted.\n"
            "  If .lattice/ is tracked in git, the file is still in git history: "
            'see "Removing media" in docs/user-reference.md.'
        )
    output_result(
        data=view,
        human_message=message,
        quiet_value=_name(view),
        is_json=is_json,
        is_quiet=quiet,
    )


def _media_before_detach(lattice_dir, raw_issue: str, raw_media: str) -> tuple | None:  # noqa: ANN001
    """The entry ``detach`` will remove and its frame count, read before the write
    (the removal drops its name from the snapshot). ``None`` when not found."""
    from lattice.storage.issue_media import list_frames
    from lattice.storage.issues import read_issue_snapshot, resolve_issue

    try:
        issue_id = resolve_issue(lattice_dir, raw_issue)
        snapshot = read_issue_snapshot(lattice_dir, issue_id) or {}
    except OpError:
        return None
    text = raw_media.strip()
    for entry in snapshot.get("media", []):
        if entry.get("removed"):
            continue
        if str(entry.get("n")) == text or str(entry.get("id", "")).lower() == text.lower():
            return entry, len(list_frames(lattice_dir, issue_id, entry))
    return None


@issue.command("media")
@click.argument("issue_id")
@click.option(
    "--paths",
    is_flag=True,
    help="Print only the files an agent can read, one per line: photos, and videos' frames.",
)
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def issue_media(issue_id: str, paths: bool, output_json: bool) -> None:
    """List an issue's photos and videos with the paths of their files.

    With --paths, print what an agent can read: each photo, and each video's
    still frames, in order. A video with no frames goes to stderr instead.
    """
    from lattice.core.events import get_actor_display
    from lattice.core.issue_media import NO_FRAMES_TEXT, format_media_lines
    from lattice.storage.issues import issue_views, read_issue_snapshot, resolve_issue

    is_json = output_json
    lattice_dir, _config = _require_issue_log(is_json)
    try:
        resolved = resolve_issue(lattice_dir, issue_id)
        snapshot = read_issue_snapshot(lattice_dir, resolved, on_unreadable=_warn_unreadable)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    if snapshot is None:
        output_error(f"Issue '{issue_id}' not found.", "NOT_FOUND", is_json)
    view = issue_views(lattice_dir, [snapshot])[0]
    present = [m for m in view.get("media", []) if not m.get("removed")]
    if is_json:
        data = {"id": view["id"], "short_id": view.get("short_id"), "media": present}
        click.echo(json_envelope(True, data=data))
        return
    if paths:
        for entry in present:
            if entry.get("missing"):
                click.echo(f"missing: {entry.get('path')}", err=True)
            elif entry.get("kind") == "photo":
                click.echo(entry["path"])
            elif entry.get("frames"):
                for frame in entry["frames"]:
                    click.echo(frame["path"])
            else:
                click.echo(f"{entry['path']}: {NO_FRAMES_TEXT}", err=True)
        return
    if not present:
        click.echo(f"{_name(view)} has no media")
        return
    click.echo(f"{_name(view)} media ({len(present)})")
    for line in format_media_lines(present, get_actor_display):
        click.echo(f"  {line}")


def _resolve_issue_description(
    description: str | None, description_file: str | None, is_json: bool
) -> str | None:
    """Resolve issue description flags, including ``-`` for stdin."""
    if description is not None and description_file is not None:
        output_error(
            "Provide either --description or --description-file, not both.",
            "VALIDATION_ERROR",
            is_json,
        )
    if description is None and description_file is None:
        return None
    value = resolve_body(
        description,
        description_file,
        is_json,
        what="issue description",
        arg_label="--description",
    )
    return _read_stdin_text() if value == "-" else value


@issue.command("edit")
@click.argument("issue_id")
@click.option("--title", default=None, help="The corrected, single-line issue title.")
@click.option(
    "--description", default=None, help="Replace the issue description; empty clears it."
)
@click.option("--description-file", default=None, help="Read the description from a file.")
@common_options
def issue_edit(
    issue_id: str,
    title: str | None,
    description: str | None,
    description_file: str | None,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Correct an issue's title or description."""
    is_json = output_json
    checked = _require_issue_log(is_json)
    description = _resolve_issue_description(description, description_file, is_json)
    lattice_dir, result = _write(
        "issue.edit",
        {
            "issue": issue_id,
            "title": title,
            "description": description,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
        checked,
    )
    view = result.value
    if result.idempotent:
        message = f"{_name(view)} is unchanged"
    else:
        data = result.events[-1]["data"]
        changed = [
            field
            for field in ("title", "description")
            if data.get(field) != data.get(f"from_{field}")
        ]
        message = f"Edited {_name(view)}: {', '.join(changed)}"
    output_result(
        data=view,
        human_message=message,
        quiet_value=_name(view),
        is_json=is_json,
        is_quiet=quiet,
    )


@issue.command("comment")
@click.argument("issue_id")
@click.argument("text", required=False)
@click.option("--file", "file_path", default=None, help="Read the comment from a file.")
@click.option("--reply-to", default=None, help="Reply to a top-level comment ID.")
@common_options
def issue_comment(
    issue_id: str,
    text: str | None,
    file_path: str | None,
    reply_to: str | None,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Add an issue comment or reply to a top-level comment."""
    is_json = output_json
    checked = _require_issue_log(is_json)
    body = resolve_body(
        text,
        file_path,
        is_json,
        what="comment text",
        arg_label="TEXT",
        missing_message="Provide comment text as TEXT or via --file.",
    )
    if body == "-":
        body = _read_stdin_text()
    _lattice_dir, result = _write(
        "issue.comment",
        {
            "issue": issue_id,
            "text": body,
            "reply_to": reply_to,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
        checked,
    )
    comment_id = result.events[-1]["id"]
    action = "Reply added" if reply_to else "Comment added"
    output_result(
        data=result.value,
        human_message=f"{action} to {_name(result.value)} ({comment_id})",
        quiet_value=comment_id,
        is_json=is_json,
        is_quiet=quiet,
    )
