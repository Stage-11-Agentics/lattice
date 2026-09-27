"""Plan-file content rules (pure)."""

from __future__ import annotations


def is_scaffold_plan(content: str, *, description: str | None = None) -> bool:
    """Return True when plan content still matches the default scaffold placeholders.

    The scaffold is minimal: just ``# <title>`` and optionally the task
    description as a paragraph.  A plan that has been "filled in" will
    contain sub-headings, lists, code fences, or other structural
    elements — OR plain-text content that differs from the auto-generated
    description paragraph.

    When *description* is provided, a plan consisting only of heading +
    that exact description text is still considered scaffold.  Without
    *description*, any non-empty text beyond the heading is accepted as
    a real plan (one-line plans are valid).
    """
    stripped = content.strip()
    if not stripped:
        return True
    lines = stripped.splitlines()
    # Must start with a heading to look like a scaffold at all.
    if not lines[0].startswith("# "):
        return False

    # Collect non-empty, non-heading body lines.
    body_lines = [lt for line in lines[1:] if (lt := line.strip())]

    if not body_lines:
        # Only a heading, no body → still scaffold.
        return True

    # If there's structural markdown content, it's definitely filled in.
    for lt in body_lines:
        if lt.startswith(("## ", "### ", "- ", "* ", "```")):
            return False
        if len(lt) > 2 and lt[0].isdigit() and ". " in lt[:5]:
            return False

    # Plain text exists. If we have the original description, check whether
    # the body is just the auto-generated description (still scaffold).
    if description:
        body_text = "\n".join(body_lines)
        desc_text = "\n".join(
            lt for line in description.strip().splitlines() if (lt := line.strip())
        )
        if body_text == desc_text:
            return True  # Body is just the description → scaffold.

    # Plain text that isn't the auto-generated description → real plan.
    return False
