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
    require_actor,
    require_root,
    resolve_task_id,
    validate_actor_format_or_exit,
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
@click.argument("source", required=False, default=None)
@click.option("--type", "art_type", default=None, help="Artifact type.")
@click.option("--title", default=None, help="Artifact title.")
@click.option("--summary", default=None, help="Short summary.")
@click.option("--sensitive", is_flag=True, help="Mark artifact as sensitive.")
@click.option("--role", default=None, help="Role of artifact on the task.")
@click.option(
    "--inline", "inline_text", default=None, help="Inline text content (instead of file/URL)."
)
@click.option("--id", "art_id", default=None, help="Caller-supplied artifact ID.")
@common_options
def attach(
    task_id: str,
    source: str | None,
    art_type: str | None,
    title: str | None,
    summary: str | None,
    sensitive: bool,
    role: str | None,
    inline_text: str | None,
    art_id: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Attach a file or URL to a task as an artifact."""
    is_json = output_json

    # Validate source/inline exclusivity
    if source is not None and inline_text is not None:
        output_error(
            "Provide either SOURCE or --inline, not both.",
            "VALIDATION_ERROR",
            is_json,
        )
    if source is None and inline_text is None:
        output_error(
            "Provide either a SOURCE (file/URL) or --inline text.",
            "VALIDATION_ERROR",
            is_json,
        )
    if inline_text is not None and art_type is not None and art_type not in {"note", "file"}:
        output_error(
            f"When using --inline, --type must be 'note' or 'file' (got '{art_type}').",
            "VALIDATION_ERROR",
            is_json,
        )

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    actor = require_actor(is_json)
    if on_behalf_of is not None:
        validate_actor_format_or_exit(on_behalf_of, is_json)

    # Validate role against configured completion policy roles
    if role is not None:
        from lattice.core.config import get_configured_roles

        configured_roles = get_configured_roles(config)
        if configured_roles and role not in configured_roles:
            output_error(
                f"Unknown role: '{role}'. Valid roles: {', '.join(sorted(configured_roles))}.",
                "INVALID_ROLE",
                is_json,
            )

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Validate task exists
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Handle inline text: write to a temp file and treat as file source
    _inline_tmp_path: Path | None = None
    if inline_text is not None:
        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, prefix="lattice-inline-"
        )
        tmp.write(inline_text)
        tmp.close()
        source = tmp.name
        _inline_tmp_path = Path(tmp.name)
        if title is None:
            title = f"Inline: {role}" if role else "inline attachment"
        if art_type is None:
            art_type = "note"

    try:
        # Determine if source is a URL or file
        assert source is not None
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

        # For file sources, verify the file exists before idempotency check
        src_path: Path | None = None
        if not is_url:
            src_path = Path(source)
            if not src_path.is_file():
                output_error(f"Source file not found: '{source}'.", "NOT_FOUND", is_json)

        # Compute expected payload filename for idempotency comparison
        if not is_url and src_path is not None:
            ext = src_path.suffix
            payload_file: str | None = f"{art_id}{ext}"
        else:
            payload_file = None

        # Idempotency check: if --id provided and metadata already exists
        # (must happen BEFORE file copy to avoid orphaned payloads on conflict)
        meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
        if meta_path.exists():
            existing = json.loads(meta_path.read_text())
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
                if existing_file != payload_file:
                    conflict = True

            if conflict:
                output_error(
                    f"Conflict: artifact {art_id} exists with different data.",
                    "CONFLICT",
                    is_json,
                )
            else:
                # Idempotent success — no file copy needed
                output_result(
                    data=existing,
                    human_message=f"Artifact {art_id} already exists (idempotent).",
                    quiet_value=art_id,
                    is_json=is_json,
                    is_quiet=quiet,
                )
                return

        # Prepare metadata kwargs
        content_type: str | None = None
        size_bytes: int | None = None
        custom_fields: dict | None = None

        if is_url:
            custom_fields = {"url": source}
        else:
            # File source: copy payload now (after idempotency check passed)
            assert src_path is not None
            dest_path = lattice_dir / "artifacts" / "payload" / f"{art_id}{src_path.suffix}"
            guessed_type, _ = mimetypes.guess_type(src_path.name)
            content_type = guessed_type
            size_bytes = src_path.stat().st_size
            shutil.copy2(str(src_path), str(dest_path))

        # Build the event first so we can use its timestamp for the artifact
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
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )

        # Build artifact metadata (use event ts for consistency)
        metadata = create_artifact_metadata(
            art_id,
            art_type,
            title,
            created_by=actor,
            created_at=event["ts"],
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

        # Apply event to snapshot
        snapshot = apply_event_to_snapshot(snapshot, event)

        # Write event and snapshot
        write_task_event(lattice_dir, task_id, [event], snapshot, config)

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
    finally:
        if _inline_tmp_path is not None:
            try:
                _inline_tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
