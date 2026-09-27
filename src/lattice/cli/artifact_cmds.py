"""Artifact commands: attach."""

from __future__ import annotations

from pathlib import Path

import click

from lattice.cli.helpers import common_options, output_error, output_result
from lattice.cli.main import cli
from lattice.cli.ops_bridge import params_or_exit, provenance_params, run_operation


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
    from lattice.ops.task_attach import encode_payload

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
    # metadata. A path that is not a readable file stays a bare SOURCE, which
    # the operation reports as not found in the order it always has.
    if source is not None and not source.startswith(("http://", "https://")):
        src_path = Path(source)
        if src_path.is_file():
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

    result = run_operation("task.attach", params, is_json)
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
