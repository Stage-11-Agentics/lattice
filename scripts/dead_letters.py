#!/usr/bin/env python3
"""Dead Letters — a memorial for cancelled Lattice tasks.

Reads the .lattice/ directory, finds every task that reached 'cancelled',
and prints a eulogy for each. The dead deserve to be remembered,
even if they were only tickets.
"""

import json
import sys
from datetime import datetime
from pathlib import Path


def find_lattice_root():
    """Walk up from cwd to find .lattice/, like git finds .git/."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".lattice").is_dir():
            return current / ".lattice"
        current = current.parent
    return None


def load_cancelled_tasks(lattice_root):
    """Find all cancelled tasks in both active and archived directories."""
    cancelled = []

    task_dirs = [
        lattice_root / "tasks",
        lattice_root / "archive" / "tasks",
    ]
    event_dirs = [
        lattice_root / "events",
        lattice_root / "archive" / "events",
    ]

    for task_dir in task_dirs:
        if not task_dir.is_dir():
            continue
        for task_file in sorted(task_dir.glob("*.json")):
            with open(task_file) as f:
                task = json.load(f)

            if task.get("status") != "cancelled":
                continue

            task_id = task["id"]
            short_id = task.get("short_id", "???")
            title = task.get("title", "Untitled")
            created_by = task.get("created_by", "unknown")
            cancelled_at = None
            cancelled_reason = None

            # Search for the cancellation event
            for event_dir in event_dirs:
                event_file = event_dir / f"{task_id}.jsonl"
                if not event_file.exists():
                    continue
                with open(event_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (
                            ev.get("type") == "status_changed"
                            and ev.get("data", {}).get("to") == "cancelled"
                        ):
                            cancelled_at = ev.get("ts")
                            cancelled_reason = ev.get("provenance", {}).get("reason")
                break  # found the event file, no need to check other dirs

            cancelled.append({
                "short_id": short_id,
                "title": title,
                "created_by": created_by,
                "cancelled_at": cancelled_at,
                "reason": cancelled_reason,
            })

    # Sort by cancellation date (None last)
    cancelled.sort(key=lambda t: t["cancelled_at"] or "9999")
    return cancelled


def format_date(iso_str):
    """Turn an ISO timestamp into a human-readable date."""
    if not iso_str:
        return "date unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y")
    except (ValueError, AttributeError):
        return "date unknown"


def format_actor(actor_str):
    """Turn 'human:atin' into 'atin' or 'agent:claude' into 'claude'."""
    if not actor_str:
        return "unknown"
    return actor_str.split(":", 1)[-1] if ":" in actor_str else actor_str


def print_memorial(cancelled):
    """Print the memorial wall."""
    width = 72

    print()
    print("┌" + "─" * width + "┐")
    print("│" + " " * width + "│")
    print("│" + "THE DEAD LETTERS".center(width) + "│")
    print("│" + "A Memorial for Cancelled Tasks".center(width) + "│")
    print("│" + " " * width + "│")

    if not cancelled:
        print("├" + "─" * width + "┤")
        print("│" + " " * width + "│")
        print("│" + "No tasks have been cancelled.".center(width) + "│")
        print("│" + "All who were born still live.".center(width) + "│")
        print("│" + " " * width + "│")
        print("└" + "─" * width + "┘")
        print()
        return

    print("│" + f"  {len(cancelled)} tasks fell before their work was done.".ljust(width) + "│")
    print("│" + " " * width + "│")
    print("├" + "─" * width + "┤")

    for i, task in enumerate(cancelled):
        sid = task["short_id"]
        title = task["title"]
        creator = format_actor(task["created_by"])
        date = format_date(task["cancelled_at"])
        reason = task["reason"]

        print("│" + " " * width + "│")

        # Short ID and title — wrap if needed
        header = f"  {sid}  \u2022  {title}"
        if len(header) > width:
            header = header[: width - 1] + "\u2026"
        print("│" + header.ljust(width) + "│")

        # Creator and date
        meta = f"    Born of {creator}  \u2022  Cancelled {date}"
        if len(meta) > width:
            meta = meta[: width - 1] + "\u2026"
        print("│" + meta.ljust(width) + "│")

        # Reason, if one was given
        if reason:
            # Word-wrap the reason
            prefix = "    \u201c"
            suffix = "\u201d"
            max_line = width - 6  # room for indent + quotes
            words = reason.split()
            lines = []
            current_line = ""
            for word in words:
                test = (current_line + " " + word).strip()
                if len(test) <= max_line:
                    current_line = test
                else:
                    if current_line:
                        lines.append(current_line)
                    current_line = word
            if current_line:
                lines.append(current_line)

            for j, line in enumerate(lines):
                if j == 0 and len(lines) == 1:
                    text = f"    \u201c{line}\u201d"
                elif j == 0:
                    text = f"    \u201c{line}"
                elif j == len(lines) - 1:
                    text = f"     {line}\u201d"
                else:
                    text = f"     {line}"
                print("│" + text.ljust(width) + "│")

        if i < len(cancelled) - 1:
            print("│" + " " * width + "│")
            print("│" + ("· " * 18).center(width) + "│")

    print("│" + " " * width + "│")
    print("├" + "─" * width + "┤")
    print("│" + " " * width + "│")
    print("│" + "They were ideas once. They mattered to someone.".center(width) + "│")
    print("│" + "Now they rest.".center(width) + "│")
    print("│" + " " * width + "│")
    print("└" + "─" * width + "┘")
    print()


def main():
    lattice_root = find_lattice_root()
    if lattice_root is None:
        print("No .lattice/ directory found.", file=sys.stderr)
        print("Run this from within a Lattice-tracked project.", file=sys.stderr)
        sys.exit(1)

    cancelled = load_cancelled_tasks(lattice_root)
    print_memorial(cancelled)


if __name__ == "__main__":
    main()
