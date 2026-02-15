"""Artifact commands: attach."""

from __future__ import annotations

import json
import mimetypes
import shutil
from pathlib import Path

import click

from lattice.cli.helpers import (
    common_options,
    load_project_config,
    output_error,
    output_result,
    read_snapshot_or_exit,
    require_root,
    resolve_actor,
    validate_actor_or_exit,
    write_task_event,
)
from lattice.cli.main import cli
from lattice.core.artifacts import (
    ARTIFACT_TYPES,
    create_artifact_metadata,
    serialize_artifact,
)
from lattice.core.events import create_event
from lattice.core.ids import generate_artifact_id, validate_id
from lattice.core.tasks import apply_event_to_snapshot
from lattice.storage.fs import atomic_write


# ---------------------------------------------------------------------------
# lattice attach
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("source")
@click.option("--type", "art_type", default=None, help="Artifact type.")
@click.option("--title", default=None, help="Artifact title.")
@click.option("--summary", default=None, help="Short summary.")
@click.option("--sensitive", is_flag=True, help="Mark artifact as sensitive.")
@click.option("--role", default=None, help="Role of artifact on the task.")
@click.option("--id", "art_id", default=None, help="Caller-supplied artifact ID.")
@common_options
def attach(
    task_id: str,
    source: str,
    art_type: str | None,
    title: str | None,
    summary: str | None,
    sensitive: bool,
    role: str | None,
    art_id: str | None,
    actor: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
) -> None:
    """Attach a file or URL to a task as an artifact."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    load_project_config(lattice_dir)  # validate config exists

    actor = resolve_actor(actor, lattice_dir, is_json)
    validate_actor_or_exit(actor, is_json)

    # Validate task exists
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Determine if source is a URL or file
    is_url = source.startswith("http://") or source.startswith("https://")

    # Infer type if not provided
    if art_type is None:
        art_type = "reference" if is_url else "file"

    if art_type not in ARTIFACT_TYPES:
        output_error(
            f"Invalid artifact type: '{art_type}'. "
            f"Valid types: {', '.join(sorted(ARTIFACT_TYPES))}.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Generate or validate artifact ID
    if art_id is not None:
        if not validate_id(art_id, "art"):
            output_error(f"Invalid artifact ID format: '{art_id}'.", "INVALID_ID", is_json)
    else:
        art_id = generate_artifact_id()

    # Derive title if not provided
    if title is None:
        if is_url:
            title = source
        else:
            title = Path(source).name

    # Prepare metadata kwargs
    payload_file: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    custom_fields: dict | None = None

    if is_url:
        # URL source: store in custom_fields, no payload file
        custom_fields = {"url": source}
    else:
        # File source: verify it exists and copy
        src_path = Path(source)
        if not src_path.is_file():
            output_error(f"Source file not found: '{source}'.", "NOT_FOUND", is_json)

        ext = src_path.suffix  # includes the dot
        dest_filename = f"{art_id}{ext}"
        dest_path = lattice_dir / "artifacts" / "payload" / dest_filename
        shutil.copy2(str(src_path), str(dest_path))

        payload_file = dest_filename
        guessed_type, _ = mimetypes.guess_type(src_path.name)
        content_type = guessed_type
        size_bytes = src_path.stat().st_size

    # Idempotency check: if --id provided and metadata already exists
    meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
    if meta_path.exists():
        existing = json.loads(meta_path.read_text())
        # Compare type + title + source info
        conflict = False
        if existing.get("type") != art_type:
            conflict = True
        elif existing.get("title") != title:
            conflict = True
        elif is_url:
            existing_url = (existing.get("custom_fields") or {}).get("url")
            if existing_url != source:
                conflict = True
        else:
            existing_file = (existing.get("payload") or {}).get("file")
            expected_file = payload_file
            if existing_file != expected_file:
                conflict = True

        if conflict:
            output_error(
                f"Conflict: artifact {art_id} exists with different data.",
                "CONFLICT",
                is_json,
            )
        else:
            # Idempotent success
            output_result(
                data=existing,
                human_message=f"Artifact {art_id} already exists (idempotent).",
                quiet_value=art_id,
                is_json=is_json,
                is_quiet=quiet,
            )
            return

    # Build artifact metadata
    metadata = create_artifact_metadata(
        art_id,
        art_type,
        title,
        created_by=actor,
        summary=summary,
        model=model,
        tags=None,
        payload_file=payload_file,
        content_type=content_type,
        size_bytes=size_bytes,
        sensitive=sensitive,
        custom_fields=custom_fields,
    )

    # Write artifact metadata atomically
    atomic_write(meta_path, serialize_artifact(metadata))

    # Build artifact_attached event
    event_data: dict = {"artifact_id": art_id}
    if role is not None:
        event_data["role"] = role

    event = create_event(
        type="artifact_attached",
        task_id=task_id,
        actor=actor,
        data=event_data,
        model=model,
        session=session,
    )

    # Apply event to snapshot
    snapshot = apply_event_to_snapshot(snapshot, event)

    # Write event and snapshot
    write_task_event(lattice_dir, task_id, [event], snapshot)

    # Output
    output_result(
        data=metadata,
        human_message=(
            f'Attached artifact {art_id} "{title}" to task {task_id}\n'
            f"  type: {art_type}  sensitive: {sensitive}"
        ),
        quiet_value=art_id,
        is_json=is_json,
        is_quiet=quiet,
    )
