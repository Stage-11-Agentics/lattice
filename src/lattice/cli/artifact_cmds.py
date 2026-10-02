"""Artifact commands: show and attach."""

from __future__ import annotations

import json
from pathlib import Path
from pathlib import PurePosixPath
from typing import NoReturn

import click

from lattice.cli.helpers import (
    common_options,
    json_envelope,
    output_error,
    output_result,
    require_root,
    validate_id,
)
from lattice.cli.main import cli
from lattice.cli.ops_bridge import (
    board_or_exit,
    caller_from_context,
    params_or_exit,
    provenance_params,
    run_operation,
)
from lattice.ops import OpError


_TEXT_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/x-javascript",
        "application/xml",
        "application/xhtml+xml",
        "application/x-csh",
        "application/x-latex",
        "application/x-sh",
        "application/x-tex",
        "application/x-texinfo",
        "application/x-troff",
        "application/x-troff-man",
        "application/x-troff-me",
        "application/x-troff-ms",
        "application/x-wais-source",
        "message/rfc822",
    }
)


@cli.group()
def artifact() -> None:
    """Read artifact metadata and payloads."""


@artifact.command("show")
@click.argument("art_id")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON.")
def artifact_show(art_id: str, output_json: bool) -> None:
    """Show an artifact and its stored payload, if available."""
    is_json = output_json
    if not validate_id(art_id, "art"):
        output_error(f"Invalid artifact ID format: '{art_id}'.", "INVALID_ID", is_json)

    # IDs are case-insensitive by validation contract. Normalize before any
    # path construction so a lowercase ID resolves on a case-sensitive disk.
    normalized_id = f"art_{art_id.split('_', maxsplit=1)[1].upper()}"
    lattice_dir = require_root(is_json)
    metadata_path = lattice_dir / "artifacts" / "meta" / f"{normalized_id}.json"
    if not metadata_path.is_file():
        output_error(f"Artifact '{normalized_id}' not found.", "NOT_FOUND", is_json)

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        output_error(
            f"Cannot read artifact metadata for '{normalized_id}': {exc}.",
            "INTEGRITY_ERROR",
            is_json,
        )
    if not isinstance(metadata, dict):
        output_error(
            f"Artifact metadata for '{normalized_id}' is not an object.",
            "INTEGRITY_ERROR",
            is_json,
        )

    payload = metadata.get("payload") or {}
    if not isinstance(payload, dict):
        _payload_unavailable(
            normalized_id, "metadata has an invalid payload record", metadata, is_json
        )

    stored_name = payload.get("file")
    content: str | None = None
    payload_path: str | None = None
    if stored_name is not None:
        safe_parts = _safe_payload_parts(stored_name)
        if safe_parts is None:
            _payload_unavailable(
                normalized_id, "metadata has an invalid payload path", metadata, is_json
            )

        try:
            lattice_root = lattice_dir.resolve()
            payload_root = (lattice_dir / "artifacts" / "payload").resolve()
            candidate = payload_root.joinpath(*safe_parts).resolve()
        except (OSError, RuntimeError) as exc:
            _payload_unavailable(
                normalized_id,
                f"payload path cannot be resolved ({exc})",
                metadata,
                is_json,
            )
        if not payload_root.is_relative_to(lattice_root) or not candidate.is_relative_to(
            payload_root
        ):
            _payload_unavailable(
                normalized_id, "metadata payload path escapes artifacts/payload", metadata, is_json
            )
        payload_path = PurePosixPath("artifacts", "payload", *safe_parts).as_posix()
        try:
            payload_bytes = candidate.read_bytes()
        except OSError as exc:
            reason = f"payload cannot be read ({exc.strerror or exc})"
            _payload_unavailable(normalized_id, reason, metadata, is_json)

        content_type = payload.get("content_type")
        if _is_text_content_type(content_type):
            try:
                content = payload_bytes.decode("utf-8")
            except UnicodeDecodeError:
                _payload_unavailable(
                    normalized_id,
                    "text payload is not valid UTF-8",
                    metadata,
                    is_json,
                )

    data = {"artifact": metadata, "content": content, "payload_path": payload_path}
    if is_json:
        click.echo(json_envelope(True, data=data))
    else:
        human_output = _artifact_human_output(metadata, content, payload_path)
        click.echo(human_output, nl=not human_output.endswith("\n"))


def _safe_payload_parts(stored_name: object) -> tuple[str, ...] | None:
    """Return safe POSIX path components for a metadata payload filename."""
    if (
        not isinstance(stored_name, str)
        or not stored_name
        or "\\" in stored_name
        or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in stored_name)
    ):
        return None
    path = PurePosixPath(stored_name)
    if not path.parts or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.parts


def _is_text_content_type(content_type: object) -> bool:
    if not isinstance(content_type, str):
        return False
    normalized = content_type.partition(";")[0].strip().lower()
    return (
        normalized.startswith("text/")
        or normalized in _TEXT_CONTENT_TYPES
        or normalized.endswith(("+json", "+xml"))
    )


def _payload_unavailable(art_id: str, reason: str, metadata: dict, is_json: bool) -> NoReturn:
    message = f"Payload for artifact '{art_id}' is unavailable: {reason}."
    if metadata.get("sensitive") is True:
        message += " The payload may not have been copied to this checkout."
    output_error(message, "PAYLOAD_UNAVAILABLE", is_json)


def _artifact_human_output(metadata: dict, content: str | None, payload_path: str | None) -> str:
    payload = metadata.get("payload") or {}
    content_type = payload.get("content_type") if isinstance(payload, dict) else None
    size_bytes = payload.get("size_bytes") if isinstance(payload, dict) else None
    artifact_id = metadata.get("id", "unknown")
    title = metadata.get("title", "")
    header = "\n".join(
        (
            f"Artifact {artifact_id}: {title}",
            f"  type: {_display_value(metadata.get('type'))}",
            f"  content_type: {_display_value(content_type)}",
            f"  size_bytes: {_display_value(size_bytes)}",
            f"  created_by: {_display_value(metadata.get('created_by'))}",
            f"  created_at: {_display_value(metadata.get('created_at'))}",
        )
    )
    if content is not None:
        return f"{header}\n\n{content}"
    if payload_path is not None:
        return (
            f"{header}\n\nBinary payload ({_display_value(content_type)}, "
            f"{_display_value(size_bytes)} bytes) at {payload_path}"
        )

    lines = [header, "", "No stored payload."]
    custom_fields = metadata.get("custom_fields")
    if isinstance(custom_fields, dict) and custom_fields.get("url") is not None:
        lines.append(f"URL: {custom_fields['url']}")
    return "\n".join(lines)


def _display_value(value: object) -> str:
    return "—" if value is None else str(value)


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
    "--criterion",
    "criterion_ids",
    multiple=True,
    help="Link this artifact evidence to a task-local acceptance criterion (repeatable).",
)
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
    criterion_ids: tuple[str, ...],
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
    from lattice.ops.task_attach import SOURCE_NOT_FOUND, encode_payload

    is_json = output_json
    params: dict = {
        "task": task_id,
        "source": source,
        "type": art_type,
        "title": title,
        "summary": summary,
        "sensitive": sensitive,
        "role": role,
        "criterion": list(criterion_ids),
        "inline": inline_text,
        "id": art_id,
        **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
    }
    # SOURCE / --inline combinations are argument errors, reported before the
    # board is looked up and before any file is read, exactly as always.
    params_or_exit("task.attach", params, is_json)

    # A readable file travels as its content (SPEC §3.8); its name is only
    # metadata. A path that is not a file stays a bare SOURCE, which the
    # operation reports as not found in the order it always has.
    board = board_or_exit(is_json)
    if source is not None and not source.startswith(("http://", "https://")):
        src_path = Path(source)
        if src_path.is_file():
            # Validate before reading: every rule that has always come before
            # the file is read (role, task, criteria, type, ID) runs first, as
            # a call with the bare SOURCE. It writes nothing, and only the
            # refusal for want of the file's content lets the read go ahead.
            try:
                board.execute("task.attach", params, caller_from_context())
            except OpError as exc:
                if exc.details.get("reason") != SOURCE_NOT_FOUND:
                    output_error(exc.message, exc.code, is_json)
            try:
                content = src_path.read_bytes()
            except OSError as exc:
                output_error(
                    f"Cannot read source file '{source}': {exc.strerror or exc}.",
                    "VALIDATION_ERROR",
                    is_json,
                )
            params["source"] = None
            params["payload"] = encode_payload(src_path.name, content)

    result = run_operation("task.attach", params, is_json, board=board)
    metadata = result.value
    output_result(
        data=metadata,
        human_message=(
            f'Attached artifact {metadata["id"]} "{metadata["title"]}" to task '
            f"{result.task['id']}\n"
            f"  type: {metadata['type']}  sensitive: {sensitive}"
            + ("  (idempotent task attachment)" if result.idempotent else "")
        ),
        quiet_value=metadata["id"],
        is_json=is_json,
        is_quiet=quiet,
    )
